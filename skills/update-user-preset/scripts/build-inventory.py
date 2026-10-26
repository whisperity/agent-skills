#!/usr/bin/env python3
"""Builds the inventory of user-originated sources in a Claude Code
configuration directory, grouped into units by the project they belong to.

Only ``<config>/projects/`` and ``<config>/history.jsonl`` are read; nothing
else in the configuration directory is walked or consulted.

A unit's key is the project's canonical root: the main repository of a Git
work tree (linked work-trees and sub-directories fold into it), or the path
itself outside of Git.
Paths that are the home directory, the file-system root, the configuration
directory, or any of its ancestors belong to the special ``<no-project>`` unit.
Project directories whose real path cannot be determined are kept as ``orphan``
units, keyed by their encoded directory name.

Each unit lists its session transcripts, its memory files, the project-level
instruction files found between the project root and the session's working
directory, and the ``history.jsonl`` lines that belong to it.
History lines are listed only if they are not slash commands and their session
has no retained transcript (otherwise the transcript is read instead); the lines
of sessions that do have a transcript, but contain pastes, are attached to that
transcript as hints to where its pastes are.

The inventory is written to the standard output as JSON, deterministically;
progress is reported on the standard error.
"""
import argparse
import enum
import functools
import itertools
import json
import operator
import os
import pathlib
import re
import subprocess
import sys
from typing import Collection, Generator, Dict, List, Set, Tuple


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="""
Scans the Claude Code configuration directory for projects and transcripts and
returns a single JSON object describing the inventory of user-originated
sources of instructions and information.
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
    projects_dir = args.config / "projects"
    global_history_file = args.config / "history.jsonl"

    inventory, sessions = scan_projects(projects_dir,
                                        global_history_file,
                                        args.config)
    assign_history(global_history_file, inventory, sessions, args.config)
    for key in inventory:
        inventory[key] = finalise_file_entries(inventory[key])

    json.dump(inventory, sys.stdout, indent=2, sort_keys=True)
    print()


NO_PROJECT = "<no-project>"


class ProjectType(enum.StrEnum):
    GIT = "git"
    PLAIN = "plain"
    MISSING = "missing"
    ORPHAN = "orphan"
    NONE = "none"


class SourceType(enum.StrEnum):
    HISTORY = "history"
    TRANSCRIPT = "transcript"
    MEMORY = "memory"
    PROJECT_INSTRUCTIONS = "project-instructions"
    USER_PROJECT_INSTRUCTIONS = "user-project-instructions"


INSTRUCTION_FILES = {
    "CLAUDE.md": SourceType.PROJECT_INSTRUCTIONS,
    "AGENTS.md": SourceType.PROJECT_INSTRUCTIONS,
    ".claude/CLAUDE.md": SourceType.PROJECT_INSTRUCTIONS,
    "CLAUDE.local.md": SourceType.USER_PROJECT_INSTRUCTIONS,
}


def scan_projects(projects_root_dir: pathlib.Path,
                  global_history_file: pathlib.Path,
                  config_dir: pathlib.Path) -> Tuple[
                      Dict, Set[str]
                  ]:
    """Scans the ``<claude_config_dir>/projects`` directory tree for
    transcripts, returning the observed inventory and the list of session IDs.
    """
    inventory: Dict = dict()
    sessions: Set[str] = set()
    if not projects_root_dir.is_dir():
        return inventory, sessions

    history_path_to_real_path = {encode_path(p): p
                                 for p in extract_project_paths_from_history(
                                     global_history_file)}
    print("Found", len(history_path_to_real_path),
          "distinct project paths in history", f"'{global_history_file}'",
          file=sys.stderr)

    for project in sorted(projects_root_dir.iterdir()):
        if not project.is_dir():
            continue
        transcripts = sorted(project.glob("*.jsonl"))
        sessions.update(str(p.stem) for p in transcripts)
        print("Found", len(transcripts), "transcripts in project",
              f"'{project.name}'",
              file=sys.stderr)

        real_path = next(
            (p for p in itertools.chain(
                (get_transcript_first_cwd(tr, project.name)
                 for tr in transcripts),
                [history_path_to_real_path.get(project.name)])
             if p is not None),
            None)
        key, kind = classify_project_root(real_path, config_dir) \
            if real_path is not None \
            else (str(project.name), ProjectType.ORPHAN)
        print("Project", f"'{project.name}'", "classified as",
              f"'{kind.value}'",
              file=sys.stderr)

        unit = inventory.setdefault(key, {
            "kind": kind.value,
            "slug": encode_path(pathlib.Path(key)),
            "files": set()})
        unit["files"].update((SourceType.TRANSCRIPT, p)
                             for p in transcripts)
        memories = sorted((project / "memory").glob("*.md"))
        unit["files"].update((SourceType.MEMORY, p)
                             for p in memories)
        print("Found", len(list(memories)), "memory files in project",
              f"'{project.name}'",
              file=sys.stderr)

        if kind in (ProjectType.GIT, ProjectType.PLAIN):
            assert real_path is not None
            for dir_ in descend_dirs(pathlib.Path(key), real_path.resolve()):
                for name, source_type in INSTRUCTION_FILES.items():
                    if (f := dir_ / name).is_file():
                        unit["files"].add((source_type, f))
                        print("Found instruction file", f"'{f}'", "in project",
                              f"'{project.name}'",
                              file=sys.stderr)

    return inventory, sessions


def assign_history(history: pathlib.Path,
                   inventory: Dict,
                   sessions: Collection[str],
                   config_dir: pathlib.Path) -> None:
    """Assigns entries in ``history.jsonl`` to projects based on the ``project``
    field.
    """
    if not history.is_file():
        return
    project_lines: Dict[str, List[int]] = dict()
    project_kinds: Dict[str, ProjectType] = dict()
    session_paste_lines: Dict[str, List[int]] = dict()

    for line_no, entry in jsonl(history):
        if (entry.get("display") or "").startswith("/"):
            continue
        if entry.get("sessionId") in sessions:
            if entry.get("pastedContents"):
                session_paste_lines.setdefault(entry["sessionId"], list()) \
                    .append(line_no)
            continue

        project = entry.get("project")
        key, kind = classify_project_root(pathlib.Path(project),
                                          config_dir) \
            if project \
            else (NO_PROJECT, ProjectType.NONE)
        project_lines.setdefault(key, list()).append(line_no)
        project_kinds.setdefault(key, kind)

    for key, line_nos in project_lines.items():
        slug = encode_path(pathlib.Path(key))
        unit = inventory.setdefault(key, {
            "kind": project_kinds[key].value,
            "slug": slug,
            "files": set()
        })
        unit["files"].add((SourceType.HISTORY, history, tuple(line_nos)))
        print("Assigned", len(line_nos), "lines from history", f"'{history}'",
              "to project", f"'{slug}'",
              file=sys.stderr)
    for unit in inventory.values():
        for f in (f for f in unit["files"]
                  if f[0] == SourceType.TRANSCRIPT
                  and len(f) == 2
                  and f[1].stem in session_paste_lines):
            session_id = f[1].stem
            file_pastes = tuple(session_paste_lines[session_id])
            unit["files"].discard(f)
            unit["files"].add((f[0], f[1], (history, file_pastes)))
            print("Assigned", len(file_pastes), "pastes from", f"'{history}'",
                  "to session", f"'{session_id}'",
                  file=sys.stderr)


def finalise_file_entries(unit: Dict) -> Dict:
    """Converts the ``files`` set in a unit to a list of dicts."""
    new_unit = {
        "kind": unit["kind"],
        "slug": unit["slug"],
        "files": [],
    }

    for f in sorted(unit["files"], key=operator.itemgetter(1)):
        assert len(f) >= 2
        type_, path = f[0], f[1]
        new_f = {
            "source": type_.value,
            "path": str(path),
        }
        if len(f) >= 3 and type_ == SourceType.HISTORY:
            new_f["lines"] = sorted(f[2])
        elif len(f) >= 3 and type_ == SourceType.TRANSCRIPT:
            paste_source_path, paste_lines = f[2][0], f[2][1]
            new_f["paste"] = {
                "path": str(paste_source_path),
                "lines": sorted(paste_lines),
            }
        new_unit["files"].append(new_f)

    return new_unit


@functools.cache
def classify_project_root(path: pathlib.Path,
                          config_dir: pathlib.Path) -> Tuple[str, ProjectType]:
    """Canonicalise and classify the project root directory at `path`."""
    path = path.resolve()
    if path == pathlib.Path.home() \
            or path == pathlib.Path(path.anchor) \
            or path.is_relative_to(config_dir) \
            or config_dir.is_relative_to(path):
        return NO_PROJECT, ProjectType.NONE
    if not path.is_dir():
        return str(path), ProjectType.MISSING
    try:
        proc = subprocess.run(
            ["git",
             "-C", str(path),
             "rev-parse",
             "--path-format=absolute",
             "--git-common-dir",
             "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        git_common_dir, toplevel = proc.stdout.strip().splitlines()
        git_common_dir_p = pathlib.Path(git_common_dir)
        toplevel_p = pathlib.Path(toplevel)

        git_root = git_common_dir_p.parent \
            if git_common_dir_p.name == ".git" \
            else toplevel_p
        if git_root == pathlib.Path.home() \
                or git_root == pathlib.Path(git_root.anchor):
            return str(git_root), ProjectType.PLAIN
        return str(git_root), ProjectType.GIT
    except (FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError):
        pass
    return str(path), ProjectType.PLAIN


def descend_dirs(base: pathlib.Path,
                 path: pathlib.Path,
                 ) -> Generator[pathlib.Path, None, None]:
    """Descends from `base` to `path`, yielding each directory along the way."""
    if path.is_relative_to(base):
        yield base
        for part in path.relative_to(base).parts:
            base = base / part
            yield base
    else:
        yield path


@functools.cache
def encode_path(path: pathlib.Path) -> str:
    """Encodes `path` as a string suitable for use as slug filename."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def extract_project_paths_from_history(history: pathlib.Path,
                                       ) -> Set[pathlib.Path]:
    """Extracts distinct project paths from the ``history.jsonl`` file."""
    return {pathlib.Path(p)
            for _, entry in jsonl(history)
            if (p := entry.get("project"))}


def get_transcript_first_cwd(file: pathlib.Path,
                             preferred_directory_name: str,
                             ) -> pathlib.Path | None:
    """Returns the first ``cwd`` value found in the transcript file that matches
    `preferred_directory_name` (to account for forked sessions or sessions moved
    between projects).
    """
    first: pathlib.Path | None = None
    try:
        with file.open(encoding="utf-8") as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    if encode_path(pathlib.Path(cwd)) \
                            == preferred_directory_name:
                        return pathlib.Path(cwd)
                    first = first or pathlib.Path(cwd)
    except OSError:
        pass
    return first


def jsonl(file: pathlib.Path) -> Generator[Tuple[int, Dict], None, None]:
    """Yields each JSON object in the `file`."""
    if not file.is_file():
        return
    with file.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield line_no, entry


if __name__ == "__main__":
    sys.exit(main() or 0)
