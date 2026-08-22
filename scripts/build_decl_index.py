#!/usr/bin/env python3
"""Build the small revision-bound declaration index used by formal_query."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterable


SCHEMA_VERSION = "decl_index_v1"
DECLARATION = re.compile(
    r"^\s*(?P<modifiers>(?:(?:@\[[^\]\n]*\]|private|protected|noncomputable|local|scoped)\s+)*)"
    r"(?P<kind>theorem|lemma|def|abbrev|structure|class|inductive|instance|axiom)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_'.₀-₉ⁿ¹²³]*)\b"
)
NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*$")
SECTION = re.compile(
    r"^\s*(?:noncomputable\s+)?section(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?\s*$"
)
END = re.compile(r"^\s*end(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?\s*$")
MUTUAL = re.compile(r"^\s*mutual\s*$")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build a deterministic SQLite declaration index from a Mathlib source tree."
    )
    result.add_argument("--source-root", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--mathlib-revision", required=True)
    result.add_argument("--lean-toolchain", required=True)
    result.add_argument("--force", action="store_true")
    return result


def _strip_line_comment(line: str) -> str:
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "-" and index + 1 < len(line) and line[index + 1] == "-":
            return line[:index]
    return line


def _statement_snippet(lines: list[str], start: int) -> str:
    pieces: list[str] = []
    for line in lines[start : start + 8]:
        value = _strip_line_comment(line).strip()
        if value:
            pieces.append(value)
        joined = " ".join(pieces)
        if ":=" in joined or re.search(r"\bwhere\b", joined):
            break
    snippet = re.sub(r"\s+", " ", " ".join(pieces)).strip()
    for marker in (":=", " where"):
        if marker in snippet:
            snippet = snippet.split(marker, 1)[0].rstrip()
    return snippet[:800]


def _qualified_name(scopes: list[tuple[str, str]], name: str) -> str:
    namespace = ".".join(value for kind, value in scopes if kind == "namespace")
    if not namespace or name.startswith(namespace + "."):
        return name
    return f"{namespace}.{name}"


def declarations(source_root: Path) -> Iterable[tuple[str, str, str, int, str, str]]:
    for path in sorted(source_root.rglob("*.lean")):
        if any(part in {".git", ".lake", "build"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        scopes: list[tuple[str, str]] = []
        relative = path.relative_to(source_root).as_posix()
        block_comment_depth = 0
        for index, raw_line in enumerate(lines):
            line = raw_line
            block_comment_depth += line.count("/-")
            if block_comment_depth:
                block_comment_depth -= line.count("-/")
                block_comment_depth = max(0, block_comment_depth)
                continue
            line = _strip_line_comment(line)
            namespace = NAMESPACE.match(line)
            if namespace:
                scopes.append(("namespace", namespace.group(1)))
                continue
            section = SECTION.match(line)
            if section:
                scopes.append(("section", section.group(1) or ""))
                continue
            if MUTUAL.match(line):
                scopes.append(("mutual", ""))
                continue
            end = END.match(line)
            if end:
                named = end.group(1)
                if named:
                    match_index = next(
                        (
                            position
                            for position in range(len(scopes) - 1, -1, -1)
                            if scopes[position][1]
                            and (
                                scopes[position][1] == named
                                or scopes[position][1].endswith(f".{named}")
                                or named.endswith(f".{scopes[position][1]}")
                            )
                        ),
                        None,
                    )
                    if match_index is not None:
                        del scopes[match_index:]
                elif scopes:
                    scopes.pop()
                continue
            match = DECLARATION.match(line)
            if not match:
                continue
            modifiers = match.group("modifiers") or ""
            if re.search(r"\b(?:private|local)\b", modifiers):
                continue
            name = _qualified_name(scopes, match.group("name"))
            snippet = _statement_snippet(lines, index)
            yield (
                name,
                match.group("kind"),
                relative,
                index + 1,
                name.rsplit(".", 1)[0] if "." in name else "",
                snippet,
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(
    source_root: Path,
    output: Path,
    *,
    mathlib_revision: str,
    lean_toolchain: str,
    force: bool,
) -> dict[str, object]:
    root = source_root.resolve()
    destination = output.resolve()
    if not root.is_dir():
        raise SystemExit("source root is not a directory")
    if destination.exists() and not force:
        raise SystemExit("output already exists; pass --force to replace it")
    revision = mathlib_revision.strip()
    toolchain = lean_toolchain.strip()
    if not revision or not toolchain:
        raise SystemExit("revision and toolchain must be non-empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_raw)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA page_size=4096")
            connection.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE decls (name TEXT NOT NULL, kind TEXT NOT NULL, file TEXT NOT NULL, "
                "line INTEGER NOT NULL, head TEXT NOT NULL, snippet TEXT NOT NULL, "
                "PRIMARY KEY (name, file, line)) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (
                    ("schema", SCHEMA_VERSION),
                    ("mathlib_revision", revision),
                    ("lean_toolchain", toolchain),
                ),
            )
            rows = sorted(declarations(root), key=lambda row: (row[0], row[2], row[3]))
            connection.executemany(
                "INSERT OR IGNORE INTO decls(name, kind, file, line, head, snippet) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
        return {
            "schema": SCHEMA_VERSION,
            "mathlib_revision": revision,
            "lean_toolchain": toolchain,
            "declaration_count": len(rows),
            "sha256": _sha256(destination),
        }
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    args = parser().parse_args()
    report = build_index(
        args.source_root,
        args.output,
        mathlib_revision=args.mathlib_revision,
        lean_toolchain=args.lean_toolchain,
        force=args.force,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
