#!/usr/bin/env python3
"""Builds the inventory of project-level memory files in a Claude Code
configuration directory.

Only ``<config>/projects/*/memory/`` is read; nothing else in the configuration
directory is walked or consulted.

Each project directory with a ``memory/`` directory is one unit, keyed by the
project directory's (encoded) name.
Units are not merged by repository: every ``memory/`` directory has its own
``MEMORY.md`` index, which is edited on its own.

Each unit lists its ``MEMORY.md`` index (if it exists), and every other memory
file, with the line numbers of the index that link to the file.
Index lines that link to a file that does not exist are listed as dangling.
Index lines that are already commented out (``<!-- ... -->``) are ignored.

The inventory is written to the standard output as JSON, deterministically;
progress is reported on the standard error.
"""
import argparse
import json
import os
import pathlib
import re
import sys
from typing import Dict, List


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="""
Scans the Claude Code configuration directory for project-level memory files,
and returns a single JSON object describing them, and their index entries.
""",
    )
    parser.add_argument("--config",
                        type=pathlib.Path,
                        default=pathlib.Path(
                            os.environ.get(
                                "CLAUDE_CONFIG_DIR",
                                str(pathlib.Path.home() / ".claude")
                            )),
                        help="path to the Claude Code configuration directory",
                        )
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    if not args.config.is_dir():
        parser.error(f"Configuration path {args.config} is not a directory.")

    inventory = scan_memories(args.config / "projects")
    json.dump(inventory, sys.stdout, indent=2, sort_keys=True)
    print()


INDEX_FILE = "MEMORY.md"
INDEX_LINK = re.compile(r"\]\((?P<target>[^)\s]+\.md)\)")


def scan_memories(projects_root_dir: pathlib.Path) -> Dict:
    """Scans every ``<projects_root_dir>/*/memory/`` directory."""
    inventory: Dict = dict()
    if not projects_root_dir.is_dir():
        return inventory

    for project in sorted(projects_root_dir.iterdir()):
        memory_dir = project / "memory"
        if not memory_dir.is_dir():
            continue
        index = memory_dir / INDEX_FILE
        links = index_links(index)
        memories = sorted(f for f in memory_dir.glob("*.md")
                          if f.name != INDEX_FILE and f.is_file())
        print("Found", len(memories), "memory files in project",
              f"'{project.name}'",
              file=sys.stderr)
        if not memories and not links:
            continue

        inventory[project.name] = {
            "memory_dir": str(memory_dir),
            "index": str(index) if index.is_file() else None,
            "memories": [{"path": str(m),
                          "index_lines": links.get(m.name, list())}
                         for m in memories],
            "dangling_index_lines": sorted(
                line
                for target, lines in links.items()
                if not (memory_dir / target).is_file()
                for line in lines),
        }

    return inventory


def index_links(index: pathlib.Path) -> Dict[str, List[int]]:
    """Maps the memory file names linked from the `index` to the line numbers
    (starting from ``1``) of the links, skipping commented out lines.
    """
    links: Dict[str, List[int]] = dict()
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return links
    for line_no, line in enumerate(lines, start=1):
        if line.lstrip().startswith("<!--"):
            continue
        for match_ in INDEX_LINK.finditer(line):
            name = pathlib.PurePosixPath(match_["target"]).name
            links.setdefault(name, list()).append(line_no)
    return links


if __name__ == "__main__":
    sys.exit(main() or 0)
