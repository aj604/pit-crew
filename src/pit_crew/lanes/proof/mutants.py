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
# A shell here-document opener: <<EOF, <<-EOF, << 'EOF', but not the here-string <<<. _delimiter reads the word.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*")
_WORD_END = frozenset(";|&<>()")  # with whitespace, what ends an unquoted shell word
_DOUBLE_QUOTE_ESCAPES = frozenset('$`"\\')  # what a backslash escapes in shell double quotes
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


class _Substitutions:
    """The shell command substitutions `$( )` open at a point, outermost first; the script itself is the first.

    A here-document's body starts after the next newline in the substitution it was opened in, so each
    keeps its own here-documents waiting for a body. Each also counts the ( it has open, so that the )
    closing a group or `(( ))` does not close the substitution.
    """

    def __init__(self) -> None:
        """Start outside any substitution."""
        self._waiting: list[list[tuple[str, bool]]] = [[]]  # (delimiter, tabs stripped)
        self._groups = [0]

    @property
    def waiting(self) -> list[tuple[str, bool]]:
        """The here-documents opened in the innermost substitution since its last newline."""
        return self._waiting[-1]

    def step(self, source: str, i: int) -> int:
        """Follow a `$(`, `(` or `)` at `i`; return how many characters it was, or 0 if none is there."""
        if source.startswith("$(", i) and not source.startswith("$((", i):
            self._waiting.append([])
            self._groups.append(0)
            return 2
        if source[i] == "(":
            self._groups[-1] += 1
        elif source[i] == ")" and self._groups[-1]:
            self._groups[-1] -= 1
        elif source[i] == ")" and len(self._groups) > 1:
            self._groups.pop()
            closed = self._waiting.pop()
            # Closed before a newline: read the bodies at the next one, which risks masking code, never a bug in text.
            self._waiting[-1] += closed
        return 1 if source[i] in "()" else 0


def _mask(source: str, *, hash_comments: bool, shell: bool = False) -> str:
    """Return the source with strings, comments and here-documents blanked to spaces; newlines and offsets kept."""
    out = list(source)
    substitutions = _Substitutions()
    i = 0
    while i < len(source):
        if source[i] == "\n" and substitutions.waiting:
            body_end = _heredoc_end(source, i + 1, substitutions.waiting)
            substitutions.waiting.clear()
            if body_end is None:  # no delimiter line: most likely not a here-document, so mask nothing
                i += 1
            else:
                _blank(out, i + 1, body_end)
                i = body_end
        elif shell and (heredoc := _heredoc(source, i)) and not _in_arithmetic(out, i):
            delimiter, tabs, opener_end = heredoc
            substitutions.waiting.append((delimiter, tabs))
            _blank(out, i, opener_end)  # a flip in a quoted delimiter would stop it matching its closing line
            i = opener_end
        elif (end := _literal_end(source, i, hash_comments=hash_comments, shell=shell)) is not None:
            _blank(out, i, end)
            if shell and source[i] == '"':
                _keep_arithmetic(out, source, i, end)
            i = end
        else:
            i += (substitutions.step(source, i) if shell else 0) or 1
    return "".join(out)


def _heredoc(source: str, i: int) -> tuple[str, bool, int] | None:
    """Read a here-document opener at `i`: return (delimiter, tabs stripped, end), or None if none is there."""
    opener = _HEREDOC.match(source, i)
    if opener is None or (word := _delimiter(source, opener.end())) is None:
        return None
    return word[0], opener.group(1) == "-", word[1]


def _delimiter(source: str, i: int) -> tuple[str, int] | None:
    r"""Read the delimiter word at `i` with its quotes removed, as Bash does: (delimiter, end), or None.

    Any part may be quoted, so E\OF, E'OF', E"O"F, $'EOF' and $"EOF" all end at a line reading EOF. A
    $'...' with a backslash in it is not read: what its escapes decode to differs between Bash versions.
    """
    start = i
    parts: list[str] = []
    while i < len(source) and not source[i].isspace() and source[i] not in _WORD_END:
        skip = 1 if source.startswith(("$'", '$"'), i) else 0  # $'...' and $"..." quote as '...' and "..."
        char = source[i + skip]
        if char in "'\"":
            quoted = _quoted(source, i + skip + 1, char, ansi=skip == 1 and char == "'")
            if quoted is None:
                return None
            part, i = quoted
        elif char == "\\" and source[i + 1 : i + 2] not in ("", "\n"):
            part, i = source[i + 1], i + 2
        elif char in "\\`":
            return None  # a line continuation, or a command substitution in the word: not a word this can read
        else:
            part, i = char, i + 1
        parts.append(part)
    return ("".join(parts), i) if i > start else None


def _quoted(source: str, i: int, quote: str, *, ansi: bool) -> tuple[str, int] | None:
    """Read a quoted part of a delimiter, from just after its opening quote: (text, end), or None."""
    parts: list[str] = []
    while i < len(source) and source[i] != quote:
        if source[i] == "\n" or (source[i] == "\\" and ansi):
            return None
        if source[i] == "\\" and quote == '"' and source[i + 1 : i + 2] in _DOUBLE_QUOTE_ESCAPES:
            i += 1
        parts.append(source[i])
        i += 1
    return ("".join(parts), i + 1) if i < len(source) else None


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
