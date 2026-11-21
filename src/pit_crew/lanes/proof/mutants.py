"""Synthetic bugs for checks with no recorded failures.

Each mutant flips one operator on one line: a comparison, a boolean, or a logical join.
Only operators the file's language has are flipped. In shell, `<` and `>` are redirects
except inside arithmetic, `(( ))` and `$(( ))`, and `and` is an ordinary word, so flipping
them anywhere else would break the script rather than plant a bug in it. Operators inside strings and
comments are left alone, and binary operators must have whitespace on both sides, which
keeps TypeScript generics (`Array<T>`) and arrows (`=>`) out. A mutant is a bug only if
the baseline CI catches it; the dispatcher replays it against the baseline first and
drops any the baseline misses.
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
_SHELL_PARENS_RE = re.compile(r"\\[^\n]|\$?\(\(|\$\(|[()]")
_HEREDOC_RE = re.compile(
    r"""<<(?P<tabs>-?)[ \t]*(?![ \t<#])(?P<word>(?:[^ \t\n;&|()<>'"\\]|\\[^\n]|'[^'\n]*'|"(?:\\[^\n]|[^"\\\n])*")+)"""
)
_DELIMITER_QUOTES_RE = re.compile(r"""\$?(?:'[^']*'|"(?:\\.|[^"\\])*")|\\.""")

type _HereDoc = tuple[str, bool, bool]


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
    code = _mask(_mask_shell_heredocs(source) if suffix == ".sh" else source, hash_comments=suffix in _HASH)
    found = []
    for number, (line, masked) in enumerate(zip(source.split("\n"), code.split("\n"), strict=True), start=1):
        operators = [*_BINARY_RE.finditer(masked), *_WORD_RE.finditer(masked)]
        arithmetic = _arithmetic(masked) if suffix == ".sh" else []
        found += [
            _mutant(path, number, line, m.start(), m.group(1))
            for m in operators
            if m.group(1) in allowed or (m.group(1) in _ORDER and any(a <= m.start() < b for a, b in arithmetic))
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


def _arithmetic(line: str) -> list[tuple[int, int]]:
    """Return the shell arithmetic regions on a line, excluding embedded commands.

    Only there do `<` and `>` compare. Inside `[[ ]]` they compare strings, where flipping
    `<` to `>=` is a syntax error, so that is left out.
    """
    spans: list[tuple[int, int]] = []
    context = [False]
    start = 0
    for match in _SHELL_PARENS_RE.finditer(line):
        was_arithmetic = context[-1]
        _shell_paren(context, match.group())
        if was_arithmetic and not context[-1]:
            spans.append((start, match.start()))
        elif not was_arithmetic and context[-1]:
            start = match.end()
    if context[-1]:
        spans.append((start, len(line)))
    return spans


def _shell_paren(context: list[bool], marker: str) -> None:
    if marker in ("((", "$(("):
        context.extend((True, True))
    elif marker == "$(":
        context.append(False)
    elif marker == "(":
        context.append(context[-1])
    elif marker == ")" and len(context) > 1:
        context.pop()


def _unquote_delimiter(match: re.Match[str]) -> str:
    quoted = match.group().removeprefix("$")
    if quoted.startswith('"'):
        return re.sub(r'\\([\\$\x60"])', r"\1", quoted[1:-1])
    return quoted[1:-1] if quoted.startswith("'") else quoted[1:]


def _heredoc_end(source: str, start: int, delimiter: str, *, strip_tabs: bool) -> int:
    while start < len(source):
        end = source.find("\n", start)
        if end == -1:
            return len(source)
        line = source[start:end]
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return end + 1
        start = end + 1
    return start


def _heredoc_scope(pending: dict[int, list[_HereDoc]], depth: int, marker: str) -> None:
    if marker == "$(":
        pending[depth] = []
    elif marker == ")":
        pending.pop(depth + 1, None)


def _mask_shell_heredocs(source: str) -> str:
    """Blank quoted here-documents, consuming all here-documents in redirection order."""
    out = list(source)
    context = [False]
    pending: dict[int, list[_HereDoc]] = {len(context): []}
    i = 0
    while i < len(source):
        documents = pending[next(reversed(pending))]
        if source[i] == "\n":
            i += 1
            for delimiter, strip_tabs, quoted in documents:
                end = _heredoc_end(source, i, delimiter, strip_tabs=strip_tabs)
                if quoted:
                    _blank(out, i, end)
                i = end
            documents.clear()
        elif paren := _SHELL_PARENS_RE.match(source, i):
            _shell_paren(context, paren.group())
            _heredoc_scope(pending, len(context), paren.group())
            i = paren.end()
        elif source.startswith("\\\n", i):
            i += 2
        elif source.startswith("<<<", i):
            i += 3
        elif not context[-1] and (opener := _HEREDOC_RE.match(source, i)):
            word = opener["word"]
            delimiter = _DELIMITER_QUOTES_RE.sub(_unquote_delimiter, word)
            documents.append((delimiter, bool(opener["tabs"]), delimiter != word))
            i = opener.end()
        else:
            i = _literal_end(source, i, hash_comments=True) or i + 1
    return "".join(out)


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
        _blank(out, i, end)
        i = end
    return "".join(out)


def _blank(out: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if out[i] != "\n":
            out[i] = " "


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
