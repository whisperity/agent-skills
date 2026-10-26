#!/usr/bin/env python3
"""Extracts the user's own words for one unit of an inventory created by
``build-inventory.py``, into text chunks meant to be read by an AI agent.

From each source of the unit:

- memory and user-project-instructions files are taken whole;
- project-instructions files are not the user's words, and are only listed in
  the header of each chunk, as context;
- history lines are taken as the user typed them, with pasted text inlined if
  it is at most ``--max-paste-chars`` long, and a placeholder otherwise;
- from transcripts, only the messages the user typed (including their feedback
  when rejecting a tool call, and skill invocations with substantial
  arguments) are taken, each with the tail of the preceding assistant response
  (at most ``--context-chars``) as context; pastes in them are located using
  the inventory's paste hints and treated the same way as in the history.

Compaction summaries, tool results, and other text injected by the harness are
not the user's words, and are never extracted.

The distilled sources (memories, user instructions) come first, followed by
the conversation in chronological order, with immediately repeated
resubmissions dropped.
The rendered messages are split into chunks of about ``--chunk-size``
characters, without ever splitting a message, and written as
``<out>/<slug>.<n>.txt``.
The standard output is a manifest of the written files with their sizes;
progress is reported on the standard error.
"""
import argparse
import dataclasses
import datetime
import enum
import functools
import json
import os
import pathlib
import re
import sys
from typing import Collection, Dict, Generator, List, Tuple


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="""
Extracts the user's messages and small enough relevant contextual information
from files mentioned in a transcript inventory JSON file and available in the
Claude Code configuration/profile directory, and writes textual output files
on which LLM-based analyses can be performed to extract facts about the user's
behaviour.
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
    parser.add_argument("--inventory",
                        type=pathlib.Path,
                        required=True,
                        help="""
Path to the transcript inventory JSON file, created by 'build-inventory.py'.
""",
                        )
    parser.add_argument("--key",
                        type=str,
                        required=True,
                        help="""
The project key (in the inventory) to run the extraction for.
One invocation of the extractor script runs one extraction of one project into
only its own output file(s).
""",
                        )
    parser.add_argument("--out",
                        type=pathlib.Path,
                        required=True,
                        help="""
Path to the output ***directory*** to write the extracted messages to.
The output is automatically written to a file named '<slug>.#.txt' in the
directory, with the '#' part replaced by a sequence number based on the chunking
of the result.
""",
                        )
    tuning = parser.add_argument_group("tuning arguments")
    tuning.add_argument("--chunk-size",
                        type=int,
                        default=150_000,
                        help="""
The number of characters to limit the size of the resulting chunks before
splitting the rest into a new file.
""",
                        )
    tuning.add_argument("--context-chars",
                        type=int,
                        default=1_024,
                        help="""
The number of characters to retain and include as assistant context before each
user message, wherever available, from the end of the AI's response, in order
to make user messages like "No, do <X> instead!" more understandable.
""",
                        )
    tuning.add_argument("--max-paste-chars",
                        type=int,
                        default=1_024,
                        help="""
The number of characters to retain and include in the extracted messages coming
from the paste actions of the user, in order to make user messages referring to
the contents of something that was pasted in more understandable.

Pastes above this limit will be replaced with a generic placeholder.
""",
                        )
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be positive")
    if args.context_chars < 0:
        parser.error("--context-chars must be non-negative")
    if args.max_paste_chars < 0:
        parser.error("--max-paste-chars must be non-negative")
    args.config = args.config.expanduser().resolve()
    if not args.config.is_dir():
        parser.error(f"Configuration path {args.config} is not a directory.")
    args.inventory = args.inventory.expanduser().resolve()
    if not args.inventory.is_file():
        parser.error(f"Inventory file {args.inventory} does not exist or is "
                     "not a file")
    try:
        unit = json.loads(args.inventory.read_text(encoding="utf-8"))[args.key]
    except json.JSONDecodeError:
        parser.error(f"Inventory file {args.inventory} is not valid JSON!")
    except KeyError:
        parser.error(f"Inventory file {args.inventory} does not contain key "
                     f"'{args.key}'")
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)

    context_files: List[pathlib.Path] = list()
    messages: List[Message] = list()
    for file in unit["files"]:
        path = pathlib.Path(file["path"])
        if not path.is_file():
            print("Warning: File", path, "does not exist or is not a file, "
                  "skipping...",
                  file=sys.stderr)
            continue
        source_type = SourceType(file["source"])

        if source_type == SourceType.PROJECT_INSTRUCTIONS:
            context_files.append(path)
            print("Registered", source_type, f"'{path}'",
                  "to be read as context.",
                  file=sys.stderr)
        else:
            file_messages = handle_source(path,
                                          source_type,
                                          file.get("lines", list()),
                                          file.get("paste"),
                                          args.config,
                                          args.context_chars,
                                          args.max_paste_chars,
                                          )
            messages += file_messages
            print("Loaded", len(file_messages), "messages from", source_type,
                  f"'{path}'",
                  file=sys.stderr)

    print(len(messages), "messages before ordering.",
          file=sys.stderr)
    messages = order_messages(messages)
    print(len(messages), "messages after ordering.",
          file=sys.stderr)
    output_chunks = to_chunks([m.render() for m in messages],
                              args.chunk_size)
    for part, body in enumerate(output_chunks, start=1):
        path = args.out / f"{unit['slug']}.{part}.txt"
        path.write_text(render_output_header(args.key,
                                             unit,
                                             context_files,
                                             part,
                                             len(output_chunks))
                        + body,
                        encoding="utf-8")
        print("Wrote part", f"#{part}", "containing", len(body),
              "characters to", f"'{path}'",
              file=sys.stderr)
        print(f"{len(body):>9}    {path}")


def jsonl(file: pathlib.Path,
          wanted_lines: Collection[int] | None = None,
          ) -> Generator[Tuple[int, Dict], None, None]:
    """Yields each JSON object in the `file`, or only those on lines present in
    `wanted_lines`.
    """
    if not file.is_file():
        return
    with file.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if wanted_lines and line_no not in wanted_lines:
                continue
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield line_no, entry


class SourceType(enum.StrEnum):
    HISTORY = "history"
    TRANSCRIPT = "transcript"
    MEMORY = "memory"
    PROJECT_INSTRUCTIONS = "project-instructions"
    USER_PROJECT_INSTRUCTIONS = "user-project-instructions"


@dataclasses.dataclass(frozen=True)
class FileReference:
    path: pathlib.Path

    def __str__(self) -> str:
        return f"file {self.path.name}"


@dataclasses.dataclass(frozen=True)
class HistoryReference:
    line: int

    def __str__(self) -> str:
        return f"line {self.line}"


@dataclasses.dataclass(frozen=True)
class TranscriptReference:
    session_id: str

    def __str__(self) -> str:
        return f"session {self.session_id[:8]}"


@dataclasses.dataclass
class Message:
    source: SourceType
    reference: FileReference | HistoryReference | TranscriptReference
    text: str
    timestamp: datetime.datetime | None = None
    context: str = ""

    def render(self) -> str:
        """Renders one message as a text block for the AI reader."""
        head = f"=== [{self.source}] {self.reference}"
        if self.timestamp is not None:
            head += f" | {self.timestamp:%Y-%m-%d %H:%M:%S} UTC"
        lines = [head]
        if self.context:
            lines += ["--- assistant (preceding, truncated)",
                      self.context,
                      "--- user",
                      ]
        lines.append(self.text.strip())
        return "\n".join(lines) + "\n\n"


PasteTemplate = Tuple[re.Pattern, List[str]]


def handle_source(file: pathlib.Path,
                  type_: SourceType,
                  lines: Collection[int],
                  paste: Dict | None,
                  config_dir: pathlib.Path,
                  context_chars: int,
                  max_paste_chars: int,
                  ) -> List[Message]:
    """Extracts the messages from one file entry mentioned in the inventory."""
    if type_ == SourceType.HISTORY:
        return handle_history(file, lines, config_dir, max_paste_chars)
    if type_ == SourceType.TRANSCRIPT:
        return handle_transcript(file,
                                 load_paste_templates(paste),
                                 context_chars,
                                 max_paste_chars)
    if type_ == SourceType.MEMORY \
            or type_ == SourceType.USER_PROJECT_INSTRUCTIONS:
        return handle_whole_file_source(file, type_)
    if type_ == SourceType.PROJECT_INSTRUCTIONS:
        raise NotImplementedError(
            "Project instructions should never distill to messages")
    print("Warning: Unknown source type", type_, "for file", file,
          "skipping...",
          file=sys.stderr)
    return []


def handle_history(file: pathlib.Path,
                   lines: Collection[int],
                   config_dir: pathlib.Path,
                   max_paste_chars: int,
                   ) -> List[Message]:
    """Extracts the messages from the global history file mentioned in the
    inventory.
    """
    messages: List[Message] = list()
    try:
        for line_no, entry in jsonl(file, set(lines)):
            text = expand_pastes(entry.get("display") or "",
                                 entry.get("pastedContents") or dict(),
                                 config_dir,
                                 max_paste_chars)
            messages.append(Message(
                source=SourceType.HISTORY,
                reference=HistoryReference(line=line_no),
                text=text,
                timestamp=to_datetime(entry.get("timestamp")),
            ))
    except OSError:
        pass
    return messages


def handle_transcript(file: pathlib.Path,
                      paste_templates: Collection[PasteTemplate],
                      context_chars: int,
                      max_paste_chars: int,
                      ) -> List[Message]:
    """Extracts the messages from a session transcript file mentioned in the
    inventory.
    """
    messages: List[Message] = list()
    last_assistant_message = ""
    try:
        for _, entry in jsonl(file):
            if entry.get("type") == "assistant":
                text = to_single_text(entry.get("message", dict())
                                      .get("content", ""))
                if text.strip():
                    last_assistant_message = text
                continue

            if (rejection_feedback := get_rejection_feedback(entry)):
                text = f"[rejected the proposed tool call] {rejection_feedback}"
            elif is_user_typed(entry):
                text = clean_skill_invocation(
                    to_single_text(entry.get("message", dict())
                                   .get("content", ""))
                )
                if text:
                    text = restore_pastes(text,
                                          paste_templates,
                                          max_paste_chars)
            else:
                continue

            if not text or not text.strip():
                continue
            ts = entry.get("timestamp")
            messages.append(Message(
                source=SourceType.TRANSCRIPT,
                reference=TranscriptReference(session_id=file.stem),
                text=text,
                timestamp=datetime_from_iso(ts),
                context=last_assistant_message[-context_chars:]
                if context_chars > 0 else "",
            ))
            last_assistant_message = ""
    except OSError:
        pass
    return messages


def handle_whole_file_source(file: pathlib.Path,
                             type_: SourceType,
                             ) -> List[Message]:
    try:
        return [Message(source=type_,
                        reference=FileReference(path=file),
                        text=file.read_text(encoding="utf-8"),
                        )]
    except OSError:
        return []


DISTILLED_SOURCES = (SourceType.MEMORY,
                     SourceType.USER_PROJECT_INSTRUCTIONS,
                     )


def order_messages(messages: Collection[Message]) -> List[Message]:
    """Orders `messages` for reading by the agent: distilled sources first
    (in inventory order), then the conversation chronologically, without
    resubmissions.
    """
    distilled = (m for m in messages if m.source in DISTILLED_SOURCES)
    conversation = [m for m in messages if m.source not in DISTILLED_SOURCES]
    conversation.sort(key=lambda m: (m.timestamp is None,
                                     m.timestamp or datetime.datetime.min
                                     .replace(tzinfo=datetime.UTC)))
    return list(distilled) + drop_resubmissions(conversation)


def to_chunks(blocks: Collection[str], max_chunk_size: int) -> List[str]:
    """Groups whole blocks of text into chunks.

    A chunk is closed after the block that reaches `max_chunk_size`, and no
    message is ever split.
    """
    chunks: List[str] = list()
    current: List[str] = list()
    size = 0
    for block in blocks:
        current.append(block)
        size += len(block)
        if size >= max_chunk_size:
            chunks.append("".join(current))
            current, size = list(), 0
    if current:
        chunks.append("".join(current))
    return chunks


def render_output_header(key: str,
                         unit: Dict,
                         context_files: Collection[pathlib.Path],
                         part: int,
                         parts: int) -> str:
    """Renders the header for one output file."""
    context = ", ".join(str(f) for f in context_files) or "no context"
    return (f"##### UNIT {key} ({unit['kind']}), part {part}/{parts}\n"
            f"##### PROJECT INSTRUCTIONS (context only, not the user's words): "
            f"{context}\n"
            "##### FORMAT: each '===' line starts one message.\n"
            "Text under '--- assistant' is only context for understanding the "
            "reply; it is never the user's words.\n"
            "Everything else presented is the user's words, including what "
            "follows an optional '--- user' prefix line.\n"
            "\n"
            )


MIN_SKILL_ARGS_CHARS = 24

NOISE_PREFIXES = ("<command-",
                  "<local-command",
                  "<task-notification>",
                  "<system-reminder>",
                  "[Request interrupted",
                  "Caveat:",
                  )

PASTE_PLACEHOLDER = re.compile(r"\[Pasted text #(?P<id>\d+)(?: \+\d+ lines)?\]")

REJECTION_FEEDBACK = re.compile(
    r"the user said:\n(?P<user_said>.*?)(?:\n\nNote: The user's next message|\Z)",
    re.S)

SKILL_INVOCATION = re.compile(
    r"<command-name>(?P<name>[^<]*)</command-name>\s*"
    r"<command-args>(?P<args>.*?)</command-args>",
    re.S)

SLASH_COMMAND = re.compile(r"/\S+\s*(?P<args>.*)", re.S)


def clean_skill_invocation(text: str) -> str | None:
    """Renders a skill invocation as ``/name args`` instead of the harness XML.

    Returns `text` unchanged if it is not a skill invocation, and None if the
    invocation's arguments are too short to carry the user's own words.
    """
    stripped = text.lstrip()
    if stripped.startswith("/"):
        match_ = SLASH_COMMAND.match(stripped)
        args = match_["args"].strip() if match_ else ""
        return stripped if len(args) >= MIN_SKILL_ARGS_CHARS else None

    if not stripped.startswith("<command-"):
        return text

    match_ = SKILL_INVOCATION.search(text)
    args = match_["args"].strip() if match_ else ""
    return f"{match_['name'].strip()} {args}" \
        if match_ and len(args) >= MIN_SKILL_ARGS_CHARS \
        else None


def drop_resubmissions(messages: Collection[Message]) -> List[Message]:
    """Drops messages that are resubmissions of the same text of a message
    directly preceding them.
    """
    kept: List[Message] = list()
    for message in messages:
        if kept and kept[-1].text.strip() == message.text.strip():
            continue
        kept.append(message)
    return kept


def expand_pastes(display: str,
                  pasted: Dict,
                  config_dir: pathlib.Path,
                  max_paste_chars: int) -> str:
    """Inlines small enough pasted texts in place of their ``[Pasted text...``
    placeholders; hash-only pastes are resolved from `config_dir` if available,
    otherwise, and for pastes larger than `max_paste_chars`, the placeholder is
    retained.
    """

    def replace(match_: re.Match) -> str:
        content = get_pasted_contents(pasted.get(match_["id"]) or dict(),
                                      config_dir / "paste-cache")
        if content is not None and len(content) <= max_paste_chars:
            return content
        return match_[0]

    return PASTE_PLACEHOLDER.sub(replace, display)


@functools.cache
def get_hash_object_contents(hash_: str, root: pathlib.Path) -> str | None:
    """Returns the contents of the hash object storage entry for `hash_` under
    the `root` directory.
    """
    try:
        return (root / f"{hash_}.txt").read_text(encoding="utf-8")
    except OSError:
        return None


def get_pasted_contents(paste: Dict, paste_cache: pathlib.Path) -> str | None:
    """Returns the contents of a `paste` action, using the `paste_cache`
    directory if the contents are remotely stored in a hash object storage.
    """
    if isinstance(paste.get("content"), str):
        return paste["content"]
    if isinstance(paste.get("contentHash"), str):
        return get_hash_object_contents(paste["contentHash"], paste_cache)
    return None


def get_rejection_feedback(entry: Dict) -> str | None:
    """Returns what the user typed in when rejecting a tool call, if any."""
    if entry.get("type") != "user":
        return None
    content = entry.get("message", dict()).get("content")
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        result = block.get("content")
        if isinstance(result, list):
            result = to_single_text(result)
        match_ = REJECTION_FEEDBACK.search(result or "")
        if match_ and match_["user_said"].strip():
            return match_["user_said"].strip()

    return None


def datetime_from_iso(text: str | None) -> datetime.datetime | None:
    """Converts a transcript's ISO 8601 ``timestamp`` into a datetime, or None
    if it is missing or malformed.
    """
    try:
        return datetime.datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def is_user_typed(entry: Dict) -> bool:
    """Returns True if the transcript entry is a genuine user message."""
    if entry.get("type") != "user":
        return False
    if "origin" in entry:
        return (entry.get("origin") or dict()).get("kind") == "human"
    if entry.get("isMeta") \
            or entry.get("isCompactSummary") \
            or entry.get("isSidechain"):
        return False

    text = to_single_text(entry.get("message", dict()).get("content")).lstrip()
    return bool(text) and not text.startswith(NOISE_PREFIXES)


def to_datetime(ms: int | None) -> datetime.datetime | None:
    """Converts ``history.jsonl`` epoch milliseconds into an aware UTC datetime.
    """
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC) \
        if ms is not None else None


def to_single_text(in_: str | List[str | Dict] | None) -> str:
    """Merges potentially split lists of text into a single string."""
    if isinstance(in_, list):
        return "\n".join(block.get("text", "") for block in in_
                         if isinstance(block, dict)
                         and block.get("type") == "text")
    return in_ or ""


def to_paste_template(display: str) -> PasteTemplate | None:
    """Turns a ``history.jsonl`` ``display`` entry into a pattern matching the
    transcript's version of the same message, where every paste placeholder is
    already expanded.
    """
    pattern: List[str] = list()
    placeholders: List[str] = list()
    previous_end: int = 0
    for match_ in PASTE_PLACEHOLDER.finditer(display):
        pattern += [re.escape(display[previous_end:match_.start()]), "(.*?)"]
        placeholders.append(match_[0])
        previous_end = match_.end()
    if not placeholders:
        return None
    pattern.append(re.escape(display[previous_end:]))
    return re.compile("".join(pattern), re.S), placeholders


def load_paste_templates(paste: Dict | None) -> List[PasteTemplate]:
    """Loads the paste templates from the ``history.jsonl`` lines in an
    inventory transcript entry's `paste` hint.
    """
    if not paste:
        return list()
    templates: List[Tuple[re.Pattern, List[str]]] = list()
    try:
        for _, entry in jsonl(pathlib.Path(paste["path"]),
                              set(paste.get("lines") or list())):
            display = entry.get("display") or ""
            if template := to_paste_template(display):
                templates.append(template)
    except OSError:
        pass
    return templates


def restore_pastes(text: str,
                   templates: Collection[PasteTemplate],
                   max_paste_chars: int) -> str:
    """Puts the placeholder back in place of pastes longer than
    `max_paste_chars`, if `text` matches one of `templates` exactly.
    """
    for pattern, placeholders in templates:
        match_ = pattern.fullmatch(text)
        if match_ is None:
            continue

        parts: List[str] = list()
        previous_end = 0
        for group, placeholder in enumerate(placeholders, start=1):
            start, end = match_.span(group)
            parts.append(text[previous_end:start])
            parts.append(match_[group]
                         if len(match_[group]) <= max_paste_chars
                         else placeholder)
            previous_end = end
        parts.append(text[previous_end:])
        return "".join(parts)
    return text


if __name__ == "__main__":
    sys.exit(main() or 0)
