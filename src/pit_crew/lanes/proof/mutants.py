"""Synthetic bugs for checks with no recorded failures.

Each mutant flips one operator on one line: a comparison, a boolean, or a logical join.
Only operators the file's language has are flipped. In shell, `<` and `>` are redirects
except inside arithmetic, `(( ))` and `$(( ))`, and `and` is an ordinary word, so flipping
them anywhere else would break the script rather than plant a bug in it. Operators inside
strings, comments and shell here-documents are left alone, and binary operators must have
whitespace on both sides, which keeps TypeScript generics (`Array<T>`) and arrows (`=>`)
out. A mutant is a bug only if the baseline CI catches it; the dispatcher replays it
against the baseline first and drops any the baseline misses.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_C_LIKE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".cc", ".cpp"}
_HASH = {".py", ".rb", ".sh"}  # languages whose comments start with #

_ORDER = frozenset({"<=", ">=", "<", ">"})
_C_OPERATORS = frozenset({"===", "!==", "==", "!=", *_ORDER, "&&", "||", "true", "false"})
# The operators a mutant may flip, by file extension.
_OPERATORS = {
    **dict.fromkeys(_C_LIKE, _C_OPERATORS),
    ".py": frozenset({"==", "!=", *_ORDER, "and", "or", "True", "False"}),
    ".rb": frozenset({"==", "!=", *_ORDER, "&&", "||", "and", "or", "true", "false"}),
    ".sh": frozenset({"==", "!=", "&&", "||", "true", "false"}),  # and _ORDER inside (( )), see _arithmetic
}

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
# A shell here-document: <<EOF, <<-EOF, << 'EOF', <<"END-OF-FILE", <<\EOF, but not the here-string <<<.
# The delimiter is the whole word; one this can't read whole (<<E"O"F) is not taken as a here-document.
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|\\?([^\s;|&<>()'"`]+))(?=[\s;|&<>()]|$)""")
_SHELL_WORD_START = " \t\n;|&()"  # where a shell # starts a comment; elsewhere ($#, ${#x}) it is code


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
    allowed = _OPERATORS.get(suffix)
    if limit < 1 or allowed is None:
        return []
    shell = suffix == ".sh"
    code = _mask(source, hash_comments=suffix in _HASH, shell=shell)
    found = []
    for number, (line, masked) in enumerate(zip(source.split("\n"), code.split("\n"), strict=True), start=1):
        operators = [*_BINARY_RE.finditer(masked), *_WORD_RE.finditer(masked)]
        arithmetic = _arithmetic(masked) if shell else [False] * len(masked)
        found += [
            _mutant(path, number, line, m.start(), m.group(1))
            for m in operators
            if m.group(1) in allowed or (m.group(1) in _ORDER and arithmetic[m.start()])
        ]
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


def _arithmetic(line: str) -> list[bool]:
    """Return, for each character of a shell line, whether it is in an arithmetic expression.

    Only in `(( ... ))` and `$(( ... ))` do `<` and `>` compare. A command substitution
    `$( ... )` inside one is ordinary shell again, where they redirect, and `[[ ]]` is never
    arithmetic: `<` there compares strings, and flipping it to `>=` is a syntax error.
    """
    inside = [False] * len(line)
    stack: list[tuple[bool, str]] = []  # (is arithmetic, what closes it), innermost last
    i = 0
    while i < len(line):
        arithmetic = bool(stack) and stack[-1][0]
        if line.startswith("$((", i) or (line.startswith("((", i) and not arithmetic):
            stack.append((True, "))"))
            i += 3 if line[i] == "$" else 2
        elif line.startswith("$(", i):
            stack.append((False, ")"))
            i += 2
        elif line[i] == "(":
            stack.append((arithmetic, ")"))  # a group keeps the context it is in
            i += 1
        elif line[i] == ")" and stack:
            _, closer = stack.pop()
            i += len(closer) if line.startswith(closer, i) else 1
        else:
            inside[i] = arithmetic
            i += 1
    return inside


def _mutant(path: str, number: int, line: str, col: int, op: str) -> Mutant:
    new = _FLIPS[op]
    return Mutant(path, number, line, line[:col] + new + line[col + len(op) :], f"{op} -> {new}")


def _mask(source: str, *, hash_comments: bool, shell: bool = False) -> str:
    """Return the source with strings, comments and here-documents blanked to spaces; newlines and offsets kept."""
    out = list(source)
    heredocs: list[tuple[str, bool]] = []  # here-documents opened on the current line: (delimiter, tabs stripped)
    i = 0
    while i < len(source):
        if source[i] == "\n" and heredocs:
            body_end = _heredoc_end(source, i + 1, heredocs)
            heredocs = []
            if body_end is None:  # no delimiter line: most likely not a here-document, so mask nothing
                i += 1
            else:
                _blank(out, i + 1, body_end)
                i = body_end
        elif shell and (heredoc := _HEREDOC.match(source, i)) and not _in_arithmetic(out, i):
            delimiter = next(word for word in heredoc.group(2, 3, 4) if word is not None)
            heredocs.append((delimiter, heredoc.group(1) == "-"))
            i = heredoc.end()
        elif (end := _literal_end(source, i, hash_comments=hash_comments, shell=shell)) is not None:
            _blank(out, i, end)
            if shell and source[i] == '"':
                _keep_arithmetic(out, source, i, end)
            i = end
        else:
            i += 1
    return "".join(out)


def _blank(out: list[str], start: int, end: int) -> None:
    for k in range(start, end):
        if out[k] != "\n":
            out[k] = " "


def _keep_arithmetic(out: list[str], source: str, start: int, end: int) -> None:
    """Put back each $(( ... )) in a double-quoted shell string: Bash still evaluates it there, unless escaped."""
    i = source.find("$((", start, end)
    while i != -1:
        depth, j = 2, i + 3
        while j < end and depth:
            depth += {"(": 1, ")": -1}.get(source[j], 0)
            j += 1
        if source[i - 1] != "\\":
            out[i:j] = source[i:j]
        i = source.find("$((", j, end)


def _in_arithmetic(out: list[str], i: int) -> bool:
    """Whether position `i`, with the source masked up to there, is in shell arithmetic, where << is a shift."""
    start = i
    while start > 0 and out[start - 1] != "\n":
        start -= 1
    return _arithmetic("".join(out[start:i]) + "<")[-1]


def _heredoc_end(source: str, start: int, heredocs: list[tuple[str, bool]]) -> int | None:
    """Return where the here-documents whose bodies start at `start` end, or None if a delimiter line is missing."""
    pos = start
    for delimiter, tabs in heredocs:
        while True:
            if pos >= len(source):
                return None
            line_end = source.find("\n", pos)
            line_end = len(source) if line_end == -1 else line_end
            line, pos = source[pos:line_end], line_end + 1
            if (line.lstrip("\t") if tabs else line) == delimiter:
                break
    return min(pos, len(source))


def _literal_end(source: str, i: int, *, hash_comments: bool, shell: bool = False) -> int | None:
    """Return where the comment or string starting at `i` ends, or None if none starts there."""
    n = len(source)
    comment = source[i] == "#" and (not shell or i == 0 or source[i - 1] in _SHELL_WORD_START)
    if (hash_comments and comment) or (not hash_comments and source.startswith("//", i)):
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
