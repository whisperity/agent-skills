---
name: update-user-preset
description: >-
  **What:** Extracts facts from the user's past and present interactive agentic
  coding sessions (transcripts) and the agent's durable project-specific
  memories to update and maintain the GLOBAL user-specific instruction preset
  file (`AGENTS.md` or `CLAUDE.md`) with new information, ensuring that the
  user's taste, preferences, and personal instructions are shared between
  projects.
  **When:** Only when the user explicitly asks for it by invoking the skill,
  never automatically as it is a costly operation.
  The user should be suggested to invoke the skill after a project-level memory
  was updated.
---

# Update User Preset

Per-project memories and per-session transcripts are scoped: what the agent
learnt about the user in one project is invisible in another.
This skill finds facts that are true of the *user* rather than specific to a
*project*: taste, working style, environment facts, preferences, and personal
instructions.
These facts are then lifted into a global preset file that is visible and shared
between projects on a harness-level, e.g., `AGENTS.md` or `CLAUDE.md`.
Placing these facts in a global preset file allows the agent to remember them
across projects and sessions.

## Preconditions

Check these preconditions before doing any extraction work.
Stop and report on a hard failure; ask the user, either interactively or via a
written question they reply to in the next prompt, if a soft failure occurs.

 * **[Hard]** Python 3.11 is required:
   `python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'`.
 * **[Hard]** The user explicitly asked for the skill to be invoked.
 * _[Soft]_ The user asked for the skill's invocation in a reasonably empty
   context, i.e., the user is not in the middle of a project-level memory update
   or any other "hard" workload.
   Preset consolidation and update should generally be its own task and session,
   but if the user insists on it being done during active development, do not
   reject it.
 * _[Soft]_ Both a `CLAUDE.md` and an `AGENTS.md` file may exist as the global
   preset (under `$CLAUDE_CONFIG_DIR`), and there could be project-specific
   similarly named files in the current project's directory.
   Never update the project-specific files!
   Before running, check and resolve which of the two global preset files is the
   one that should be updated — if ambiguous, warn and ask the user.
 * _[Soft]_ The user may be cheeky and have their global preset files not as
   real character files, but as symbolic links to some other location.
   Check this, and if so, write the file that the symbolic link (chain) points
   to; do not cobble and overwrite the link itself with a text file.

## Steps

The skill runs in **five steps**, detailed in the Workflow:

 1. **Preparation**: resolve the global preset file and back it up.
 2. **Inventory**: list, deterministically, every source of the user's words,
    grouped by project.
 3. **Extraction**: extract the user's words per project, distil facts from
    them in parallel and in isolation, and verify the facts against the words.
 4. **Synthesis**: consolidate the verified facts of all projects, together
    with the existing preset, into a proposed new preset, keeping track of the
    provenance of every change.
 5. **Review and commit**: show the user the difference, and write the
    approved result to the global preset file.

## Rules

 * Consider the user's already established data in the state files of the
   harness **read-only**.
   Transcripts should not be modified in any way.
   Write the result into a temporary output directory, e.g.,
   `$CLAUDE_JOB_DIR/tmp/` or a system-created temporary directory (`mktemp -d`),
   and ask the user to review and approve the changes before committing them to
   the global preset file.
 * The global preset file is a single source of truth that gets injected into
   every new session from thereon — its existing content should not be
   invalidated or contradicted without evidence, and especially it must never
   become corrupted.
 * As the processing of the transcripts and memories is costly and potentially
   time-consuming, and because preferences in one project may conflict with
   those in another, run as many of the steps in parallel using sub-agents.
   The sub-agents should use a cheap, fast, and low-effort model, e.g.,
   "Claude Haiku" on "Medium", to run the fact extraction.
   Preferences extracted from "project A" should not influence "project B"'s
   extraction process, but the sum of the two should both influence the final
   result.
 * The step where facts extracted from individual projects are synthesised into
   the global preset runs in a **single sub-agent**, using the most capable
   model available, e.g., "Claude Opus" on "High" effort level.
   It is never split across sub-agents, because it must see information from
   every project at once.

## Workflow

Let `$CONFIG` be the configuration directory
(`${CLAUDE_CONFIG_DIR:-$HOME/.claude}`) and `$JOB` be the job directory
(`${CLAUDE_JOB_DIR}/tmp` or `mktemp -d`) chosen according to the Rules.
Let `<skill-dir>` be the base directory where this skill is installed.

### 1. Preparation

 1. Resolve the global preset file according to the Preconditions, and remember
    both the file path loaded by the harness and the real resolved file path in
    case the former is a symbolic link.
 2. Copy the contents of the global preset file (with symbolic link resolution)
    to `$JOB/AGENTS.ORIG.md` as a backup and as a reference base-line for a
    later diff.

### 2. Build inventory

 1. Run the inventory builder script, and save its output to the job directory:

    ```bash
    python3 -I <skill-dir>/scripts/build-inventory.py --config "$CONFIG" \
      > "$JOB/inventory.json"
    ```
 2. The output of the file will be a single JSON mapping that maps from
    "normalised project roots" (project directories normalised to the top-level
    canonical Git repository they are contained in, or the raw paths if none
    such exists) to a data structure where:

    - `kind`: identifies the kind of the source path/entry (the key's kind)
    - `slug`: a slugified version of the key, suitable for use as a file name,
      extracted or generated from the existing key path
    - `files`: a list of file objects pointing to files relevant to the fact
      extraction process (see _Step 3._):

      - `source`: the type marker of the source file
      - `path`: the path of the file mentioned in the current record
      - `lines`: a list of line numbers in the file that are relevant to the
        fact extraction process (the first line is `1`, the field is relevant
        and present only if `source` is `history`)
      - `paste`: a record of two components, describing and connecting the
        user's "paste" actions for messages in a **session** (whereby the
        transcript already contains the expansion of the pasted content) to an
        authoritative source of the paste (the field is relevant and present
        only if `source` is `transcript`):

        - `path`: path to the file where the paste marker and associated
          information in the record is explicitly present, this is almost always
          the global `history.jsonl` file
        - `lines`: a list of line numbers in the file (pointed to by `path`,
          starting from `1`), which contains the aforementioned paste markers

    Key **`kind`** may be one of:

    - `git`: a Git repository's canonical root directory, with inner work-trees
      normalised to the canonical top-level root (if the repository and the
      work-tree still exists)
    - `plain`: a plain path existing on the file-system but outside the scope of
      Git version control
    - `missing`: the real path is known (from a `cwd` in the transcript(s) or
      explicitly from a history entry), but does not exist on the file-system,
      e.g., the deleted sub-directory or a work-tree of another project
    - `orphan`: the real path not known, only a slugified
      `$CLAUDE_CONFIG_DIR/projects/...` path exists, no `cwd` in the
      transcript(s) and no history entry with matching directory path; the
      result will be keyed by the encoded directory name
    - `none`: the path is `$HOME`, `/`, `$CLAUDE_CONFIG_DIR` or anything inside
      it or any of its parent directories (e.g., `~/.claude`), or entirely
      omitted; the result will be keyed under a special extremal value
      `<no-project>`

    File **`source`** type may be one of:

    - `history`: the global history file, usually filtered to specific `lines`
      only, that requires specific additional parsing and processing; only the
      lines that are not `/`-commands and are not already contained in a
      `transcript` file are retained and marked potentially relevant here
    - `transcript`: the per-session transcript files, that require a normal
      amount of parsing and processing, but contain the most relevant
      information
    - `memory`: existing project-level `memory/` files curated by the harness
      already, along with the _`MEMORY.md`_ index file
    - `project-instructions`: existing project-level instruction files curated
      by the developers of the project; these will need to be investigated in
      detail to establish context, as a "standing `CLAUDE.md` rule" mentioned,
      e.g., in a _memory_ file usually refers to the project-level instruction,
      not the user's global instruction file
    - `user-project-instructions`: existing, conventionally unversioned
      project-level instructions explicitly written and maintained by the local
      user (owning the project check-out) rather than the project itself, e.g.,
      a `CLAUDE.local.md` file

    **`project-instructions`** files are NOT the user's own words, the claims
    therein should be noted but skipped in the synthesis process.
    It is important that only the user's own instructions and preferences are
    extracted to the global preset file, and it does not get tainted by a single
    project's own conventions and rules.

    Example:

    ```json
    {
        "/Users/username/project": {
            "kind": "git",
            "slug": "-Users-username-project",
            "files": [
                {
                    "source": "memory",
                    "path": ".../memory/feedback_style.md"
                },
                {
                    "source": "history",
                    "path": ".../history.jsonl",
                    "lines": [3, 17, 18]
                },
                {
                    "source": "transcript",
                    "path": ".../8bded1bb-d776-4a7e-82c9-2d8e8581429a.jsonl",
                    "paste": {
                        "path": ".../history.jsonl",
                        "lines": [1234]
                    }
                }
            ]
        }
    }
    ```

 3. If the script fails for any reason (non-zero exit code), stop and report it.
    Do NOT improvise the inventory with shell globs or other heuristics!
 4. Show the user a summary, and wait.
    One line per unit: key, kind, and the number of memories, transcripts, and
    history lines found.
    This is the last "cheap" point to abort the skill, before the expensive
    fact extraction process begins.

### 3. Extract the user's words, and distil facts about the user's preferences

 1. For each unit in the inventory (`<key>` of the dictionary serialised to
    `inventory.json`, e.g., `/Users/username/project`), execute the **message
    extraction script**, preferably in parallel in the background.
    Do not run the script on the same `<key>` more than once.
    Do not spawn sub-agents to run the script – do it in the "main" agent or
    the "main" process of the session.

    The _standard output_ of the script will print a manifest of the files and
    the sizes thereof that were written to the output directory.

    ```bash
    python3 -I <skill-dir>/scripts/extract-user-messages.py \
      --config "$CONFIG" \
      --inventory "$JOB/inventory.json" \
      --key="<key>" \
      --out "$JOB/messages/"
    ```

    The script will write some files under `--out`, e.g.,
    `$JOB/messages/<slug>.#.txt`, where `<slug>` is the slugified project root
    key as present in the `inventory.json` file, and the `#` is replaced by a
    sequence number (starting from `1`) for each file written by the script
    in case the output is too large to fit in a single file.
    A single file with `.1.txt` as the suffix will always exist as long as there
    are any messages extracted for the given key.
    Additional files `.2.txt`, etc., may or may not exist.

    Wait until all such message extraction scripts have finished executing.
    If no messages were extracted for any of the found `<key>` units, or there
    are no entries in the inventory, stop and report this to the user.
 2. Spawn **sub-agents** to perform fact extraction and the assigning of
    judgement to the extracted facts.
    Use a cheap available model line for the fact extraction.
    Start all sub-agents in parallel, at once.
    The instructions for the sub-agent are present in
    **`references/fact-extraction.md`**.
    Provide the instruction file's path to the sub-agent and instruct it to read
    its instruction by reading the file fully.
    Each sub-agent should be given a single input file from the outputs of the
    previous step (indicated in the output manifest), and write the output to a
    corresponding `.txt` file, e.g., `-Users-username-project.1.txt`,
    under `$JOB/facts/`.
    The sub-agent must not consult any other files (e.g., the inventory, the
    existing global preset file, or any other file containing extracted
    messages) except the contextual files explicitly mentioned in the input
    file.
    The sub-agent should reply with the output path it wrote, the number of
    facts it extracted, and a tally of the marks and evidence types it used.
    Continue only when all sub-agents are done.
    Re-run a failed sub-agent only once, and report if it fails again.
    Never fill in what should have been its findings heuristically, but continue
    with the rest of the workflow once all sub-agents have finished, as long as
    one managed to produce some facts.
    If no facts were extracted from the messages at all, stop, and report this
    to the user.
 3. Verify the _extracted facts_ against the message chunks, and continue with
    the verified copies **only**.
    This should be done by the main session, not by sub-agents.

    ```bash
    python3 -I <skill-dir>/scripts/verify-facts.py \
      --facts "$JOB/facts" \
      --messages "$JOB/messages" \
      --out "$JOB/facts-verified"
    ```

    A fact is kept only if it is well-formed, uses the allowed marks, and its
    quote is the user's verbatim words (never `assistant` context) in the
    message its reference names.
    A reference naming a non-existent message is repaired when the quote
    identifies the message unambiguously.
    Rejected lines are written to `<chunk>.rejected.txt` next to the verified
    copy, with the reason; the standard output lists the kept counts and the
    verdicts per file.
    Do not re-add rejected facts by hand!

### 4. Consolidate the extracted facts into a single user-level preference file

 1. Spawn **one** sub-agent, using the most capable model available (see the
    Rules), with the instructions in **`references/synthesis.md`**.
    This agent should be given the following inputs, and nothing else:

    - the current global preset('s snapshot): `$JOB/AGENTS.ORIG.md`, which may
      not exist
    - the verified facts: every `*.txt` file in `$JOB/facts-verified/`,
      excluding the `*.rejected.txt` files
    - the instruction to write its outputs to `$JOB/AGENTS.NEW.md` and
      `$JOB/report.md`

    It must not read transcripts, message chunks, the inventory, or the real
    global preset file, and it must write nothing but the two output files.
 2. The sub-agent should write the **complete** proposed preset to
    `$JOB/AGENTS.NEW.md`, and then write `$JOB/report.md` from that file and
    the decisions it made, such that every changed line in the preset appears in
    the report, and every change in the report appears in the preset.
    The report lists, for every added, changed, or removed line, the facts and
    project references backing it, the conflicts that could not have been
    resolved, the project-specific facts that were not promoted, and the details
    on the suggested new skills.
    The sub-agent replies with only the two paths and the number of lines
    added, changed, and removed.
 3. If the sub-agent fails, re-run it **once**, if it fails again, stop and
    report this fact.
    Do not synthesise the preset or any of the contents of the report yourself.
 4. Do not write the global preset file in this step!
    The diff between the two and the review are in the next step, for the user
    to do.

### 5. Review and commit

 1. Compute the difference yourself, deterministically; do not rely on the
    report for it:

    ```bash
    diff -u "$JOB/AGENTS.ORIG.md" "$JOB/AGENTS.NEW.md"
    ```

    (Use `/dev/null` in place of `AGENTS.ORIG.md` if the preset did not exist.)
    If there is no difference, tell the user, and stop.
 2. Cross-check the diff against the **Changes** section of the report: every
    changed line of the diff must be explained there, and every change listed
    there must be in the diff.
    Report any mismatch to the user, as it is; do not repair either file
    yourself.
 3. Show the user the diff, and a summary of the report: the changes with their
    reasons and backing projects, the conflicts, the number of facts not
    promoted per project, the suggested skills, and the gaps.
    Ask the user to approve, reject, or edit.

    - On **edits**, apply exactly the edits the user asked for to
      `$JOB/AGENTS.NEW.md`, show the new diff, and ask again.
      Do not make any other change.
    - On **rejection**, write nothing, and stop; the outputs remain in `$JOB`.

 4. Once approved, check that the global preset file has not changed since it
    was backed up in _Step 1._: its current contents must be identical to
    `$JOB/AGENTS.ORIG.md`.
    If it has changed, do not write it; show the user how, and stop.
 5. Write the contents of `$JOB/AGENTS.NEW.md` to the **resolved real file** of
    the global preset (see _Step 1._), replacing its contents.
    Never replace a symbolic link in the chain with a regular file.
 6. Verify the write: read the file back through the path the harness loads,
    and check that it is identical to `$JOB/AGENTS.NEW.md` (e.g., `cmp`).
    If it is not, restore the contents of `$JOB/AGENTS.ORIG.md` to the real
    file, and report the failure.
 7. Report to the user: which file was written, where the backup of the
    previous version is, and that the backup is in a temporary directory that
    may be cleaned up.
    If the real file is under version control, say so, and leave committing it
    to the user.
    Mention that the facts which were not promoted, and the suggested skills,
    are in `$JOB/report.md`.
