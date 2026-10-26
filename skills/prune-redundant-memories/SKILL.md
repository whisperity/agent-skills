---
name: prune-redundant-memories
description: >-
  **What:** Finds project-level `memory/` files whose claims are already covered
  by the user's GLOBAL instruction preset (`AGENTS.md` or `CLAUDE.md`), and
  suggests keeping, trimming, or deleting each of them, leaving a dated marker
  of every change.
  **When:** Only when the user explicitly asks for it by invoking the skill,
  typically after the global preset was updated (e.g., by `update-user-preset`).
---

# Prune Redundant Memories

Per-project memories record what an agent learnt in one project.
Once the same knowledge is part of the global preset, which every session loads
anyway, the memory entry becomes a duplicate: it costs context in every session
of its project, and it can drift out of sync with the preset.
This skill compares each memory file with the global preset, and trims or
deletes what the preset already says.

## Preconditions

Check these preconditions before doing any work.
Stop and report on a hard failure; ask the user, either interactively or via a
written question they reply to in the next prompt, if a soft failure occurs.

 * **[Hard]** Python 3.11 is required:
   `python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'`.
 * **[Hard]** The user explicitly asked for the skill to be invoked.
 * **[Hard]** The global preset exists, is not blank, and contains preferences
   and instructions.
   Resolve it as below; if neither `CLAUDE.md` nor `AGENTS.md` exists under
   `$CLAUDE_CONFIG_DIR`, or the resolved file is empty or white space only,
   report that there is nothing to compare the memories against, so no action
   is needed, and stop.
 * _[Soft]_ The user asked for the skill's invocation in a reasonably empty
   context, i.e., not in the middle of other work in a project whose memories
   may be changed.
 * _[Soft]_ Both a `CLAUDE.md` and an `AGENTS.md` file may exist as the global
   preset (under `$CLAUDE_CONFIG_DIR`), and there could be project-specific
   similarly named files in the current project's directory.
   Only the global preset is compared against; never a project-specific file.
   If it is ambiguous which of the two global files is the preset, warn and
   ask the user.
 * _[Soft]_ The global preset may be a symbolic link (chain) to another
   location; read the file the chain points to.

## Rules

 * The global preset is **read-only** for this skill.
 * Only the memory files listed in the inventory, and their own `MEMORY.md`
   index files, may be changed — and only **after the user approved the
   change**.
 * Never delete or change a memory file only because it is old, short, or
   seems unimportant: the only reason this skill acts upon something is if the
   global preset already says the same.
 * Every change leaves a marker, so that the user, and later agents, can tell
   what was removed, when, and why.
 * Decide a verdict for each memory file independently of every other, and
   cheaply: run it in parallel sub-agents, using the cheapest available model
   but with reasonable effort, e.g., "Claude Haiku" on "Medium".

## Workflow

Let `$CONFIG` be the configuration directory
(`${CLAUDE_CONFIG_DIR:-$HOME/.claude}`) and `$JOB` be the job directory
(`${CLAUDE_JOB_DIR}/tmp` or `mktemp -d`).
Let `<skill-dir>` be the base directory where this skill is installed.

### 1. Preparation

 1. Resolve the global preset file according to the Preconditions.
    If it does not exist or is empty, stop, as described there.
 2. Copy its contents to `$JOB/PRESET.md`; the jurors read this snapshot, never
    the real file.

### 2. Build inventory

 1. Run the inventory builder script, and save its output to the job directory:

    ```bash
    python3 -I <skill-dir>/scripts/build-inventory.py --config "$CONFIG" \
      > "$JOB/inventory.json"
    ```

    If the script fails (non-zero exit code), stop and report it.
    Do not improvise the inventory with shell globs.
 2. The output maps each project directory's name to:

    - `memory_dir`: the project's `memory/` directory;
    - `index`: its `MEMORY.md` index file, or `null` if there is none;
    - `memories`: every memory file (except the index), each with its `path`,
      and `index_lines`: the line numbers (starting from `1`) of the index that
      link to it;
    - `dangling_index_lines`: index lines linking to a file that does not
      exist (reported, never changed by this skill).

    If the inventory is empty, report that there are no memories, and stop.
 3. Copy every listed memory directory to `$JOB/backup/<project>/`, preserving
    the files as they are.
    This will allow the user to recover any memory files with their original
    contents, even after the user allowed the modification-applicator script to
    change or delete them in the "live" file system (the real `memory/`
    directories).

### 3. Calculate a verdict for every memory file

 1. Spawn one juror sub-agent for **each** memory file in the inventory, all at
    once, in parallel, using the cheapest available model (see the Rules).
    The sub-agent's instructions can be found in **`references/judgement.md`**,
    which path must be given to the sub-agent and it should read the
    instructions from it completely.
    In addition to the instructions file's path, notify the sub-agent about the
    following paths as well:

    - the preset at `$JOB/PRESET.md`
    - the memory file to judge: its `path` from the inventory, e.g.,
      `$CONFIG/projects/<project>/memory/<file>.md`
    - the path where the sub-agent should write its verdict:
      `$JOB/verdicts/<project>.<file>.txt`, where `<file>` is the memory
      file's name without its `.md` extension (e.g.,
      `feedback_no-git.md`'s verdict is written to
      `<project>.feedback_no-git.txt`)

 2. If a sub-agent fails, re-run it once; if it fails again, its memory is
    treated as `keep` (a missing verdict file means `keep` for that memory).
    Never judge a memory file yourself!
 3. Verify the verdicts, and compute the plan, without changing anything:

    ```bash
    python3 -I <skill-dir>/scripts/apply-verdicts.py \
      --inventory "$JOB/inventory.json" \
      --preset "$JOB/PRESET.md" \
      --verdicts "$JOB/verdicts" \
      --backup "$JOB/backup"
    ```

    The script checks every verdict deterministically, and downgrades it to
    `keep` if it is malformed, cites nothing, cites text that is not in the
    preset (compared with white space collapsed), or is an `update` without an
    updated memory.
    Its standard output is the _judgement plan_: every memory file with its
    final verdict, the reason of any downgrade, the citations, the index lines
    to comment out, and a diff for every `update`.
    An `update` whose new body is a _single line_, or starts with a _"Why"_ or
    a _"How to apply"_, is flagged with a warning: point these out to the user
    in the review, as the remaining memory may not make sense on its own.
    An `update` that rewords the memory (its new body contains lines that were
    not in the original) is flagged too: the jurors may rephrase, but must not
    change the meaning, so point the user to the rephrased lines in the diff.
    If no verdict file is found for **any** of the memory files, the script
    exits with a non-zero code instead: the verdicts were not written, or not
    where, or not as named, as described above.
    Stop and report this; do not treat it as "nothing to prune"!

### 4. Review

Show the user the plan, as the script printed it.
Point out every warning in it, especially the conflicts: a memory claim that
contradicts the preset is either a deliberate exception of its project, or a
stale habit, and only the user can tell which.
Ask the user to approve all of the changes, some of them, or none — on any
doubt, a file is kept.
Nothing is changed before approval, and do not change the plan yourself.

### 5. Apply

Run the previous (_Step 3._) command with the additional **`--apply`** flag,
and, unless the user approved _every_ change, with an **`--only`** flag
specifying exactly which memory files' changes were approved:

```bash
python3 -I <skill-dir>/scripts/apply-verdicts.py \
  --inventory "$JOB/inventory.json" \
  --preset "$JOB/PRESET.md" \
  --verdicts "$JOB/verdicts" \
  --backup "$JOB/backup" \
  --apply \
  --only="<project>/<memory file name without .md>" \
  --only="<project>/<another memory file name without .md>" \
  ...
```

Always write `--only=` with the `=`: project names may start with `-`, and would
otherwise be read as an option.
Like the verdict files, `--only` takes the memory file's name **without its
`.md` extension**, as the plan prints it (e.g.,
`--only="<project>/feedback_no-git"`).
The script uses one UTC time-stamp for the whole run, and:

 * **`keep`**: changes nothing.
 * **`update`** (partial cover): keeps the front matter of the memory file (and
   sets its _`modified`_ field, if any, to the time-stamp), then writes a
   marker, and the approved **`UPDATED MEMORY`** as the new body:

   ```markdown
   <!-- prune-redundant-memories <timestamp>: claims covered by the global preset were removed; covered by: "<preset rule>"; "<preset rule>" -->
   ```

 * **`delete`** (full cover): deletes the memory file, and replaces each of its
   index lines in the project's _`MEMORY.md`_ index with the same line,
   commented out, which removes it from the rendered index, but keeps the
   marker:

   ```markdown
   <!-- prune-redundant-memories <timestamp>: deleted, covered by the global preset: "<preset rule>"; "<preset rule>" | <original index line> -->
   ```

A memory file, or an index, that changed since its backup is skipped, and the
script exits with a non-zero code; report every skipped file.
Never edit the memory files or the index by yourself manually when executing
this skill!

### 6. Report

Re-run the inventory script to a different output file, e.g.,
`$JOB/inventory.post.json`, and confirm that every deleted file is gone, and
that no index line links to a deleted file any more (`dangling_index_lines` is
empty for the file's corresponding record).

Report, per project: the files kept, updated, and deleted; the verdicts that
were downgraded, and why; the files skipped because they changed; the dangling
index lines found; and that the previous contents are in `$JOB/backup/`, a
temporary directory that may need the user's explicit, manual clean-up.
