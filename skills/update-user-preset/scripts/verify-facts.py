#!/usr/bin/env python3
"""Checks the fact lines written by the fact extraction sub-agents against the
message chunks they were extracted from, and keeps only the verifiable ones.

A fact line is kept if it is well-formed, uses only the allowed marks, and its
quote appears verbatim in the user's own words (never in assistant context) of
the message its reference names.
A reference that names a non-existent message is repaired if the quote appears
in exactly one message.
"""
import argparse
import pathlib
import re
import sys
from typing import Dict, List, Mapping, Tuple


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="""
Verifies the LLM-extracted facts against the message chunks extracted from the
user's inputs, own words, to prevent hallucinations or context from the provided
assistant messages from being introduced as factual evidence.
""",
    )
    parser.add_argument("--facts",
                        type=pathlib.Path,
                        required=True,
                        help="""
Directory of '<chunk>.txt' fact files.
""",
                        )
    parser.add_argument("--messages",
                        type=pathlib.Path,
                        required=True,
                        help="""
Directory of '<chunk>.txt' message chunks.
""",
                        )
    parser.add_argument("--out",
                        type=pathlib.Path,
                        required=True,
                        help="""
Directory to write the verified fact files to.
Should not be the same as the previous two directories.
""",
                        )
    args = parser.parse_args()
    for dir_ in (args.facts, args.messages):
        if not dir_.expanduser().is_dir():
            parser.error(f"{dir_} is not a directory")
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    facts_files = sorted(f for f in args.facts.expanduser().iterdir()
                         if f.suffix == ".txt"
                         and not f.name.endswith(".rejected.txt"))
    for facts_file in facts_files:
        chunk_file = args.messages.expanduser() / f"{facts_file.stem}.txt"
        if not chunk_file.is_file():
            print(f"{facts_file.name}: no matching chunk, skipped",
                  file=sys.stderr)
            continue
        messages = split_messages(chunk_file.read_text(encoding="utf-8"))
        kept: List[str] = list()
        rejected: List[str] = list()
        verdicts: Dict[str, int] = dict()
        for line in facts_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.strip() == "(nothing)":
                continue
            fixed, verdict = check_line(line, messages)
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if fixed is None:
                rejected.append(f"{line}\n  # rejected: {verdict}")
            else:
                kept.append(fixed)
        (out / facts_file.name).write_text(
            "\n".join(kept) + "\n" if kept else "(nothing)\n", encoding="utf-8")
        if rejected:
            (out / f"{facts_file.stem}.rejected.txt").write_text(
                "\n".join(rejected) + "\n", encoding="utf-8")
        tally = ", ".join(f"{v}: {n}" for v, n in sorted(verdicts.items()))
        print(f"{len(kept):>4} kept    {out / facts_file.name}    "
              f"({tally or 'empty'})")


MARKS = {"permanent", "durable", "correction"}
EVIDENCE = {"CORRECTION", "EXPLICIT", "INFERRED"}
FACT_LINE = re.compile(
    r'^- \[(?P<mark>[^\]]+)\] \[(?P<evidence>[^\]]+)\] (?P<fact>.+?)'
    r' \| "(?P<quote>.*)" \| (?P<reference>.+?) \| ×(?P<count>\d+)'
    r'(?P<hint> \| hint: project)?\s*$')
CONTEXT_BLOCK = re.compile(
    r"--- assistant \(preceding, truncated\)\n.*?\n--- user\n", re.S)


def split_messages(chunk: str) -> Dict[str, str]:
    """Maps each message's ``===`` header (without ``=== ``) to the user's own
    words in that message, with any assistant context removed.

    Headers are not unique (two messages of a session in the same second share
    one), so the words of all messages under the same header are kept.
    """
    messages: Dict[str, str] = dict()
    for block in re.split(r"(?m)^=== ", chunk)[1:]:
        header, _, body = block.partition("\n")
        words = CONTEXT_BLOCK.sub("", body)
        messages[header.strip()] = messages.get(header.strip(), "") + words
    return messages


def check_line(line: str, messages: Mapping[str, str]) -> Tuple[str | None, str]:
    """Returns the (possibly repaired) line, or None, and the verdict."""
    match_ = FACT_LINE.match(line)
    if match_ is None:
        return None, "malformed"
    if match_["mark"] not in MARKS or match_["evidence"] not in EVIDENCE:
        return None, "unknown mark or evidence"
    quote = match_["quote"].replace('\\"', '"')
    reference = match_["reference"].strip()
    if quote in messages.get(reference, ""):
        return rebuild(line, match_, quote, match_["reference"]), "ok"
    holders = [ref for ref, words in messages.items() if quote in words]
    if len(holders) == 1:
        return rebuild(line, match_, quote, holders[0]), "reference repaired"
    if not holders:
        return None, "quote not found in the user's words"
    return None, "quote ambiguous, reference wrong"


def rebuild(line: str, match_: re.Match, quote: str, reference: str) -> str:
    """Returns `line` with its quote and reference fields replaced, by their
    positions, so that the same text elsewhere in the line is left untouched.
    """
    quote_start, quote_end = match_.span("quote")
    reference_start, reference_end = match_.span("reference")
    return (line[:quote_start] + quote
            + line[quote_end:reference_start] + reference
            + line[reference_end:])


if __name__ == "__main__":
    sys.exit(main() or 0)
