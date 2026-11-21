"""Synthetic bugs for checks with no recorded failures.

Each mutant flips one operator on one line: a comparison, a boolean, or a logical join.
Operators inside strings and comments are left alone, and binary operators must have
whitespace on both sides, which keeps TypeScript generics (`Array<T>`) and arrows (`=>`)
out. A mutant is a bug only if the baseline CI catches it; the dispatcher replays it
against the baseline first and drops any the baseline misses.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_C_LIKE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".cc", ".cpp"}
_HASH = {".py", ".rb", ".sh"}

# Longest first so "===" is not read as "==".
_BINARY = [
    ("===", "!=="), ("!==", "==="), ("==", "!="), ("!=", "=="),
    ("<=", ">"), (">=", "<"), ("<", ">="), (">", "<="),
    ("&&", "||"), ("||", "&&"), ("and", "or"), ("or", "and"),
]
_WORDS = {"true": "false", "false": "true", "True": "False", "False": "True"}
_BINARY_RE = re.compile(r"(?<=\s)(" + "|".join(re.escape(op) for op, _ in _BINARY) + r")(?=\s)")
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b")
_FLIP = dict(_BINARY)


@dataclass(frozen=True)
class Mutant:
    path: str
    line: int  # 1-based
    before: str
    after: str
    operator: str  # e.g. "== -> !="


def mutants(path: str, source: str, limit: int = 50) -> list[Mutant]:
    """One-operator mutants of a source file, spread evenly across it, at most `limit`."""
    suffix = PurePosixPath(path).suffix
    if suffix not in _C_LIKE | _HASH:
        return []
    code = _mask(source, hash_comments=suffix in _HASH)
    found = []
    for number, (line, masked) in enumerate(zip(source.split("\n"), code.split("\n"), strict=True), start=1):
        for match in _BINARY_RE.finditer(masked):
            op = match.group(1)
            if op in ("and", "or") and suffix not in _HASH:  # words, not operators, outside Python/Ruby/sh
                continue
            found.append(_mutant(path, number, line, match.start(), op, _FLIP[op]))
        for match in _WORD_RE.finditer(masked):
            op = match.group(1)
            found.append(_mutant(path, number, line, match.start(), op, _WORDS[op]))
    if len(found) <= limit:
        return found
    step = len(found) / limit
    return [found[int(i * step)] for i in range(limit)]


def apply_mutant(source: str, mutant: Mutant) -> str:
    lines = source.split("\n")
    if lines[mutant.line - 1] != mutant.before:
        raise ValueError(f"{mutant.path}:{mutant.line} no longer matches the mutant")
    lines[mutant.line - 1] = mutant.after
    return "\n".join(lines)


def _mutant(path: str, number: int, line: str, col: int, op: str, new: str) -> Mutant:
    return Mutant(path, number, line, line[:col] + new + line[col + len(op) :], f"{op} -> {new}")


def _mask(source: str, hash_comments: bool) -> str:
    """The source with strings and comments blanked to spaces; newlines and offsets kept."""
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = source[i]
        if hash_comments and c == "#" or not hash_comments and source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
        elif not hash_comments and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
        elif source.startswith(('"""', "'''"), i) and hash_comments:
            end = source.find(source[i : i + 3], i + 3)
            end = n if end == -1 else end + 3
        elif c in "\"'`":
            end = i + 1
            while end < n and source[end] != c and not (source[end] == "\n" and c != "`"):
                end += 2 if source[end] == "\\" else 1
            end = min(end + 1, n)
        else:
            i += 1
            continue
        blank(i, end)
        i = end
    return "".join(out)
