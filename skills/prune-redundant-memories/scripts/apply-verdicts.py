#!/usr/bin/env python3
"""Verifies the judges' verdicts on memory files, shows the resulting plan, and
applies the approved part of it.

For every memory file in an inventory created by ``build-inventory.py``, the
verdict is read from ``<verdicts>/<project>.<file>.txt``, in the form:

    VERDICT: keep | update | delete
    COVERED BY:
    - "<a passage of the preset>"
    CONFLICTS WITH:
    - "<a passage of the preset a remaining claim contradicts>"
    UPDATED MEMORY:
    <the new body of the memory file, for "update" only>

A verdict is downgraded to ``keep`` if it is missing or malformed, if it cites
nothing, if any cited text is not found in the preset (compared with whitespace
collapsed, so a rule wrapped over several lines still matches), or if an
``update`` has no new body.
A line holding several quotes is split into them, if every one of them is
found in the preset.
Verified conflicts are only shown as a warning; they never change the verdict.

Without ``--apply``, nothing is changed: the plan is printed on the standard
output, with a diff for every update.
With ``--apply``, the plan is executed, for the memory files named with
``--only`` (or for all of them, if ``--only`` is not given):

- ``update``: the body of the memory file is replaced, after its front matter,
  with a marker comment followed by the new body; a ``modified`` field of the
  front matter is set to the run's timestamp;
- ``delete``: the memory file is deleted, and each line of the ``MEMORY.md``
  index linking to it is replaced by a marker comment containing the original
  line.

A file, or an index, that differs from its copy in ``--backup`` is not changed.
Every marker carries the same UTC timestamp, the skill's name, and the cited
preset text.
"""
import argparse
import dataclasses
import datetime
import difflib
import json
import pathlib
import re
import sys
from typing import Collection, Dict, List, Literal, Set


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="""
Verifies, shows, and applies keep/update/delete verdicts on memory files.
""",
    )
    parser.add_argument("--inventory",
                        type=pathlib.Path,
                        required=True,
                        help="""
The inventory created by 'build-inventory.py'.
""",
                        )
    parser.add_argument("--preset",
                        type=pathlib.Path,
                        required=True,
                        help="""
The snapshot of the global preset.
""",
                        )
    parser.add_argument("--verdicts",
                        type=pathlib.Path,
                        required=True,
                        help="""
The directory of the verdict files.
""",
                        )
    parser.add_argument("--backup",
                        type=pathlib.Path,
                        required=True,
                        help="""
The directory of the memory directories' copies, as
'<backup>/<project>/<file>.md'.
""",
                        )
    parser.add_argument("--apply",
                        action="store_true",
                        help="""
Actually execute the plan (default: without it, only show the plan as a
dry-run).
""",
                        )
    parser.add_argument("--only",
                        action="append",
                        default=list(),
                        metavar="PROJECT/FILE-WITHOUT-MD-EXTENSION",
                        help="""
With '--apply', change only the memory files mentioned via this flag, with the
'.md' extension removed (repeatable, default: change all files according to
the plan).
""",
                        )
    args = parser.parse_args()
    for path in (args.inventory, args.preset):
        if not path.expanduser().is_file():
            parser.error(f"{path} is not a file")
    if args.only and not args.apply:
        parser.error("'--only' requires '--apply'")

    inventory = json.loads(args.inventory.expanduser().read_text(
        encoding="utf-8"))
    preset = collapse(args.preset.expanduser().read_text(encoding="utf-8"))
    plans = [verify(project, unit, memory, args.verdicts.expanduser(), preset)
             for project, unit in sorted(inventory.items())
             for memory in unit["memories"]]

    if plans and all(p.note == "no verdict file" for p in plans):
        print(f"No verdict file was found in '{args.verdicts}' for any of the "
              f"{len(plans)} memory files; check the verdict files' names.",
              file=sys.stderr)
        return 1

    unknown = set(args.only) - {p.name for p in plans}
    if unknown:
        parser.error(f"not in the inventory: {', '.join(sorted(unknown))}")

    for plan in plans:
        print(plan.render())
    if not args.apply:
        print("Dry run: nothing was changed.",
              file=sys.stderr)
        return 0

    approved: Set[str] = set(args.only) or {p.name for p in plans}
    timestamp = datetime.datetime.now(datetime.UTC) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return not apply(plans, approved, args.backup.expanduser(), timestamp)


SKILL = "prune-redundant-memories"
VERDICTS = ("keep", "update", "delete")
VERDICT_LINE = re.compile(r"^VERDICT:\s*(?P<verdict>\w+)\s*$", re.M | re.I)
COVERED_HEADER = re.compile(r"^COVERED BY:\s*$", re.M | re.I)
CONFLICTS_HEADER = re.compile(r"^CONFLICTS WITH:\s*$", re.M | re.I)
UPDATED_HEADER = re.compile(r"^UPDATED MEMORY:\s*$", re.M | re.I)
CITATION = re.compile(r'^\s*-\s*"(?P<text>.*)"\s*$')
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
MODIFIED_FIELD = re.compile(r"^(?P<key>\s*modified:\s*).*$", re.M)
INDEX_LINK = re.compile(r"\]\((?P<target>[^)\s]+\.md)\)")
EXPLANATION_ONLY = re.compile(r"\A\s*\**\s*(Why|How to apply)\s*:", re.I)


@dataclasses.dataclass
class Plan:
    project: str
    memory: pathlib.Path
    index: pathlib.Path | None
    index_lines: List[int]
    verdict: Literal["keep"] | Literal["update"] | Literal["delete"] = "keep"
    citations: List[str] = dataclasses.field(default_factory=list)
    new_body: str = ""
    note: str = ""

    @property
    def name(self) -> str:
        return f"{self.project}/{self.memory.stem}"

    def render(self) -> str:
        """Renders one plan entry for review."""
        lines = [f"=== {self.name}: {self.verdict.upper()}"]
        if self.note:
            lines.append(f"    ({self.note})")
        lines += [f"    covered by: \"{c}\"" for c in self.citations]
        if self.verdict == "delete":
            lines.append(f"    index lines to comment out: {self.index_lines}")
        if self.verdict == "update":
            try:
                old = FRONT_MATTER.sub("",
                                       self.memory.read_text(encoding="utf-8"))
            except OSError:
                old = ""
                lines.append("    (the memory file can not be read)")
            lines += [f"    {d}" for d in difflib.unified_diff(
                old.strip().splitlines(), self.new_body.splitlines(),
                "current", "updated", lineterm="")]
        return "\n".join(lines) + "\n"


def verify(project: str,
           unit: Dict,
           memory: Dict,
           verdicts_dir: pathlib.Path,
           preset: str) -> Plan:
    """Reads and verifies the verdict on one memory file."""
    path = pathlib.Path(memory["path"])
    plan = Plan(project=project, memory=path,
                index=pathlib.Path(unit["index"]) if unit["index"] else None,
                index_lines=list(memory["index_lines"]))
    verdict_file = verdicts_dir / f"{project}.{path.stem}.txt"
    try:
        text = verdict_file.read_text(encoding="utf-8")
    except OSError:
        plan.note = "no verdict file"
        return plan

    match_ = VERDICT_LINE.search(text)
    verdict = match_["verdict"].lower() if match_ else ""
    if verdict not in VERDICTS:
        plan.note = "malformed verdict"
        return plan
    conflicts = [c for c in quotes(section(text, CONFLICTS_HEADER,
                                           [UPDATED_HEADER]), preset)
                 if collapse(c) in preset]
    conflict_warning = ["warning: a remaining claim conflicts with the "
                        "preset: " + "; ".join(f'"{c}"' for c in conflicts)] \
        if conflicts else list()
    if verdict == "keep":
        plan.note = "; ".join(conflict_warning)
        return plan

    plan.citations = quotes(section(text, COVERED_HEADER,
                                    [CONFLICTS_HEADER, UPDATED_HEADER]),
                            preset)
    new_body = section(text, UPDATED_HEADER, list()).strip()
    if not plan.citations:
        plan.note = f"'{verdict}' cites nothing"
    elif missing := [c for c in plan.citations
                     if collapse(c) not in preset]:
        plan.note = f"cited text not in the preset: \"{missing[0]}\""
    elif verdict == "update" and not new_body:
        plan.note = "'update' without an updated memory"
    else:
        plan.verdict, plan.new_body = verdict, new_body
        warnings = update_warnings(path, new_body) \
            if verdict == "update" \
            else list()
        plan.note = "; ".join(warnings + conflict_warning)
        return plan
    plan.citations = list()
    plan.note = "; ".join([f"downgraded to 'keep': {plan.note}"]
                          + conflict_warning)
    return plan


def apply(plans: Collection[Plan],
          approved: Collection[str],
          backup_dir: pathlib.Path,
          timestamp: str) -> bool:
    """Executes the approved, changing part of `plans`."""
    failures = 0
    index_edits: Dict[pathlib.Path, Dict[int, str]] = dict()
    deleted: Dict[pathlib.Path, Set[str]] = dict()
    for plan in plans:
        if plan.verdict == "keep" or plan.name not in approved:
            continue
        if drifted(plan.memory, backup_dir / plan.project / plan.memory.name):
            print(f"Skipped {plan.name}: changed since the backup",
                  file=sys.stderr)
            failures += 1
            continue
        rules = "; ".join(f'"{c}"' for c in plan.citations)
        if plan.verdict == "update":
            write_update(plan, timestamp, rules)
            print(f"Updated {plan.name}",
                  file=sys.stderr)
            continue

        if plan.index and plan.index_lines and drifted(
                plan.index, backup_dir / plan.project / plan.index.name):
            print(f"Skipped {plan.name}: its index changed since the backup",
                  file=sys.stderr)
            failures += 1
            continue
        plan.memory.unlink()
        print(f"Deleted {plan.name}",
              file=sys.stderr)
        if plan.index:
            deleted.setdefault(plan.index, set()).add(plan.memory.name)
            for line in plan.index_lines:
                index_edits.setdefault(plan.index, dict())[line] = \
                    f"{SKILL} {timestamp}: deleted, covered by the global " \
                    f"preset: {rules}"

    for index, edits in index_edits.items():
        lines = index.read_text(encoding="utf-8").splitlines()
        commented = 0
        for line_no, marker in edits.items():
            line = lines[line_no - 1]
            targets = {pathlib.PurePosixPath(m["target"]).name
                       for m in INDEX_LINK.finditer(line)}
            if not targets <= deleted[index]:
                print(f"Left line {line_no} of {index} unchanged: it also "
                      f"links memory files that were not deleted",
                      file=sys.stderr)
                continue
            lines[line_no - 1] = comment(f"{marker} | {line}")
            commented += 1
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Commented out {commented} line(s) of {index}",
              file=sys.stderr)

    return failures == 0


def update_warnings(memory: pathlib.Path, new_body: str) -> List[str]:
    """Returns the reasons an updated memory needs the user's closer look."""
    warnings: List[str] = list()
    if len(new_body.splitlines()) == 1 or EXPLANATION_ONLY.match(new_body):
        warnings.append("warning: the updated memory may not stand on its own "
                        "(a single line, or only a 'Why'/'How to apply')")
    try:
        old_body = FRONT_MATTER.sub("", memory.read_text(encoding="utf-8"))
    except OSError:
        old_body = ""
    old_text = collapse(old_body)
    if any(collapse(line) not in old_text
           for line in new_body.splitlines() if line.strip()):
        warnings.append("warning: memory was reworded; check that the meaning "
                        "did not change")
    return warnings


def write_update(plan: Plan,
                 timestamp: str,
                 rules: str) -> None:
    """Replaces the body of the memory file, keeping its front matter."""
    old = plan.memory.read_text(encoding="utf-8")
    match_ = FRONT_MATTER.match(old)
    front_matter = match_[0] if match_ else ""
    front_matter = MODIFIED_FIELD.sub(lambda m: m["key"] + timestamp,
                                      front_matter)
    marker = comment(f"{SKILL} {timestamp}: claims covered by the global "
                     f"preset were removed; covered by: {rules}")
    plan.memory.write_text(
        f"{front_matter}\n{marker}\n\n{plan.new_body}\n", encoding="utf-8")


def section(text: str,
            start: re.Pattern,
            ends: Collection[re.Pattern]) -> str:
    """Returns the text between the `start` header and the first of the `ends`
    headers that follows it (or the end of the text).
    """
    begin = start.search(text)
    if begin is None:
        return ""
    finishes = [m.start() for end in ends
                if (m := end.search(text, begin.end()))]
    return text[begin.end():min(finishes, default=len(text))]


def quotes(block: str, preset: str) -> List[str]:
    """Returns the quotes of the ``- "..."`` lines of a verdict section.

    A line holding several quotes (``"..." "..."``) is split into them, but
    only if every one of them is in the `preset`: a quote is never accepted
    unless it is verified.
    """
    found: List[str] = list()
    for line in block.splitlines():
        match_ = CITATION.match(line)
        if match_ is None:
            continue
        text = match_["text"]
        parts = text.split('" "')
        if collapse(text) not in preset \
                and len(parts) > 1 \
                and all(collapse(p) in preset for p in parts):
            found += parts
        else:
            found.append(text)
    return found


def collapse(text: str) -> str:
    """Collapses every run of whitespace into a single space."""
    return " ".join(text.split())


def comment(text: str) -> str:
    """Wraps `text` in an HTML comment, keeping the comment valid."""
    return f"<!-- {text.replace('--', '- -')} -->"


def drifted(file: pathlib.Path, backup: pathlib.Path) -> bool:
    """Whether `file` differs from its `backup`, or the backup is missing."""
    try:
        return file.read_bytes() != backup.read_bytes()
    except OSError:
        return True


if __name__ == "__main__":
    sys.exit(main() or 0)
