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
_WORD_OPERATORS = {"and", "or"}  # operators only in Python, Ruby and shell

# Longest first, so "===" is never read as "==".
_BINARY = {
    "===": "!==",
    "!==": "===",
    "==": "!=",
    "!=": "==",
    "<=": ">",
    ">=": "<",
    "<": ">=",
    ">": "<=",
    "&&": "||",
    "||": "&&",
    "and": "or",
    "or": "and",
}
_WORDS = {"true": "false", "false": "true", "True": "False", "False": "True"}
_FLIPS = _BINARY | _WORDS
_BINARY_RE = re.compile(r"(?<=\s)(" + "|".join(re.escape(op) for op in _BINARY) + r")(?=\s)")
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b")


@dataclass(frozen=True)
class Mutant:
    """A one-line bug: replace line `line` (1-based) of `path`, which reads `before`, with `after`."""

    path: str
    line: int
    before: str
    after: str
    operator: str  # e.g. "== -> !="


class StaleMutantError(ValueError):
    """Raised when the line a mutant targets has changed since the mutant was made."""

    def __init__(self, mutant: Mutant) -> None:
        """Name the line that no longer matches."""
        super().__init__(f"{mutant.path}:{mutant.line} no longer matches the mutant")


def mutants(path: str, source: str, limit: int = 50) -> list[Mutant]:
    """Return one-operator mutants of a source file, spread evenly across it, at most `limit`."""
    suffix = PurePosixPath(path).suffix
    if limit < 1 or suffix not in _C_LIKE | _HASH:
        return []
    hash_comments = suffix in _HASH
    code = _mask(source, hash_comments=hash_comments)
    found = []
    for number, (line, masked) in enumerate(zip(source.split("\n"), code.split("\n"), strict=True), start=1):
        operators = [m for m in _BINARY_RE.finditer(masked) if hash_comments or m.group(1) not in _WORD_OPERATORS]
        operators += _WORD_RE.finditer(masked)
        found += [_mutant(path, number, line, m.start(), m.group(1)) for m in operators]
    if len(found) <= limit:
        return found
    step = len(found) / limit
    return [found[int(i * step)] for i in range(limit)]


def apply_mutant(source: str, mutant: Mutant) -> str:
    """Return `source` with the mutant's line replaced. Raises StaleMutantError if the line changed."""
    lines = source.split("\n")
    if not 1 <= mutant.line <= len(lines) or lines[mutant.line - 1] != mutant.before:
        raise StaleMutantError(mutant)
    lines[mutant.line - 1] = mutant.after
    return "\n".join(lines)


def _mutant(path: str, number: int, line: str, col: int, op: str) -> Mutant:
    new = _FLIPS[op]
    return Mutant(path, number, line, line[:col] + new + line[col + len(op) :], f"{op} -> {new}")


def _mask(source: str, *, hash_comments: bool) -> str:
    """Return the source with strings and comments blanked to spaces; newlines and offsets kept."""
    out = list(source)
    i = 0
    while i < len(source):
        end = _literal_end(source, i, hash_comments=hash_comments)
        if end is None:
            i += 1
            continue
        for k in range(i, end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def _literal_end(source: str, i: int, *, hash_comments: bool) -> int | None:
    """Return where the comment or string starting at `i` ends, or None if none starts there."""
    n = len(source)
    if (hash_comments and source[i] == "#") or (not hash_comments and source.startswith("//", i)):
        end = source.find("\n", i)
        return n if end == -1 else end
    if not hash_comments and source.startswith("/*", i):
        end = source.find("*/", i + 2)
        return n if end == -1 else end + 2
    if hash_comments and source.startswith(('"""', "'''"), i):
        end = source.find(source[i : i + 3], i + 3)
        return n if end == -1 else end + 3
    if source[i] in "\"'`":
        quote, end = source[i], i + 1
        while end < n and source[end] != quote and not (source[end] == "\n" and quote != "`"):
            end += 2 if source[end] == "\\" else 1
        return min(end + 1, n)
    return None
