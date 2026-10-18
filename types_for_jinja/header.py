"""Parse ``{#def ... #}`` type declarations from a Jinja template.

The first one is the template's own context header. A later one inside a
``{% macro %}`` body types that macro's parameters, and may include one
``caller(...)`` clause for the call block.

The patterns are built from the project's configured delimiters, so a Jinja superset
that changes them still gets its headers read.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, replace
from functools import lru_cache

from types_for_jinja.config import Syntax


@lru_cache(maxsize=8)
def _patterns(syntax: Syntax) -> tuple[re.Pattern[str], re.Pattern[str]]:
    open_c, close_c = re.escape(syntax.comment_start_string), re.escape(syntax.comment_end_string)
    open_b = re.escape(syntax.block_start_string)
    return (
        re.compile(rf'{open_c}-?\s*def\b(?P<body>.*?)-?{close_c}', re.DOTALL),
        re.compile(rf'{open_b}-?\s*macro\b'),
    )


@dataclass(frozen=True)
class Param:
    """One declared context name, with its annotation and default when the header gives them.

    ``annotation`` is ``None`` for a bare name, which JinjaX allows. Each emitter picks its own
    stand-in, because a generated stub and a generated wrapper spell ``Any`` differently.
    """

    name: str
    annotation: str | None = None
    default: str | None = None

    def annotated(self, fallback: str) -> str:
        """``name: annotation``, falling back to ``fallback`` for an untyped name."""
        return f'{self.name}: {self.annotation or fallback}'


@dataclass(frozen=True)
class CallerClause:
    """The ``caller(...)`` signature of a macro's call block.

    ``returns`` is the annotation after ``->``, when the clause has one. Slot parameters use the
    same shape as the macro's own parameters.
    """

    params: list[Param]
    returns: str | None = None


@dataclass(frozen=True)
class TemplateHeader:
    """The typed context a template declares via its ``{#def ... #}`` comment."""

    imports: list[str]
    params: list[Param]
    lineno: int
    malformed: str | None = None
    """Why the parameter list could not be read, when it could not."""
    caller: CallerClause | None = None
    """The call block's signature, when a macro header declares ``caller(...)``."""


_TWO_CALLER_CLAUSES = 'malformed {#def #} header: more than one caller clause'
_CALLER_AND_PARAMETER = 'malformed {#def #} header: caller is both a parameter and a caller clause'
_CALLER_ON_TEMPLATE = 'malformed {#def #} header: a caller clause belongs on a macro'
_INVALID_CALLER = 'malformed {#def #} header: invalid caller clause'


def parse_header(source: str, syntax: Syntax | None = None) -> TemplateHeader | None:
    """Return the template's own ``{#def ... #}`` header, or ``None`` if it has none.

    A block that follows the first ``{% macro %}`` types that macro's parameters, not the
    template, so a macros-only file reports no header rather than borrowing one. A
    ``caller(...)`` clause belongs on a macro, so here it is reported and left unset.
    """
    header_re, macro_re = _patterns(syntax or Syntax())
    match = header_re.search(source)
    if match is None:
        return None
    macro = macro_re.search(source)
    if macro is not None and macro.start() < match.start():
        return None
    header = _parse_block(match, source)
    if header.caller is None:
        return header
    return replace(header, caller=None, malformed=header.malformed or _CALLER_ON_TEMPLATE)


def parse_defs(source: str, syntax: Syntax | None = None) -> list[TemplateHeader]:
    """Return every ``{#def ... #}`` declaration in ``source``, in source order."""
    header_re, _ = _patterns(syntax or Syntax())
    return [_parse_block(match, source) for match in header_re.finditer(source)]


def _parse_block(match: re.Match[str], source: str) -> TemplateHeader:
    imports: list[str] = []
    declarations: list[str] = []
    for raw in match.group('body').splitlines():
        line = raw.strip().rstrip(',')
        if not line:
            continue
        if line.startswith(('import ', 'from ')):
            imports.append(line)
        else:
            declarations.append(line)
    remaining, caller, malformed = _lift_caller(', '.join(declarations))
    params, param_error = _parse_params(remaining)
    if malformed is None and caller is not None and any(param.name == 'caller' for param in params):
        caller = None
        malformed = _CALLER_AND_PARAMETER
    elif malformed is None:
        malformed = param_error
    return TemplateHeader(
        imports=imports,
        params=params,
        lineno=source.count('\n', 0, match.start()) + 1,
        malformed=malformed,
        caller=caller,
    )


_OPENS = frozenset('([{')
_CLOSES = {')': '(', ']': '[', '}': '{'}


def _lift_caller(text: str) -> tuple[str, CallerClause | None, str | None]:
    """Lift one top-level ``caller(...)`` clause out of a declaration list.

    A second clause is an error. The clause's slots and return go through the same signature
    parse as the other declarations.
    """
    clauses: list[str] = []
    rest: list[str] = []
    for segment in _split_top_level(text):
        stripped = segment.strip()
        if _is_caller_clause(stripped):
            clauses.append(stripped)
        else:
            rest.append(segment)
    if len(clauses) > 1:
        return _join(rest), None, _TWO_CALLER_CLAUSES
    if not clauses:
        return text, None, None
    caller, error = _parse_caller(clauses[0])
    return _join(rest), caller, error


def _join(segments: list[str]) -> str:
    return ', '.join(segment.strip() for segment in segments)


def _is_caller_clause(text: str) -> bool:
    """A clause is ``caller`` followed by ``(``, not a parameter whose name is ``caller``."""
    if not text.startswith('caller'):
        return False
    return text[len('caller') :].lstrip().startswith('(')


def _split_top_level(text: str) -> list[str]:
    """Split on commas that sit outside parentheses, brackets, braces, and quotes."""
    parts: list[str] = []
    start = 0
    depth = dict.fromkeys(_OPENS, 0)
    index = 0
    while index < len(text):
        nxt = _consume_quoted(text, index)
        if nxt != index:
            index = nxt
            continue
        char = text[index]
        if char in depth:
            depth[char] += 1
        elif char in _CLOSES and depth[_CLOSES[char]]:
            depth[_CLOSES[char]] -= 1
        elif char == ',' and not any(depth.values()):
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _consume_quoted(text: str, index: int) -> int:
    """Return the index after a quote that starts at ``index``, or ``index`` when none does."""
    quote = text[index]
    if quote not in {'"', "'"}:
        return index
    token = quote * 3 if text.startswith(quote * 3, index) else quote
    cursor = index + len(token)
    while cursor < len(text):
        if text.startswith(token, cursor):
            return cursor + len(token)
        if len(token) == 1 and text[cursor] == '\\':
            cursor += 2
            continue
        cursor += 1
    return len(text)


def _parse_caller(clause: str) -> tuple[CallerClause | None, str | None]:
    split = _split_clause(clause)
    if split is None:
        return None, _INVALID_CALLER
    inside, returns = split
    function = _parsed_function(inside, returns)
    if function is None:
        return None, f'malformed {{#def #}} header: {clause!r} is not a valid parameter list'
    parsed = ast.unparse(function.returns) if function.returns is not None else None
    return CallerClause(_params_of(function.args), parsed), None


def _split_clause(clause: str) -> tuple[str, str | None] | None:
    rest = clause[len('caller') :].lstrip()
    end = _matching_paren(rest)
    if end is None:
        return None
    tail = rest[end + 1 :].strip()
    if not tail:
        return rest[1:end], None
    if not tail.startswith('->'):
        return None
    returns = tail[2:].strip()
    if not returns:
        return None
    return rest[1:end], returns


def _matching_paren(text: str) -> int | None:
    """Index of the ``)`` that closes a ``(`` at the start of ``text``."""
    if not text.startswith('('):
        return None
    depth = 0
    index = 0
    while index < len(text):
        nxt = _consume_quoted(text, index)
        if nxt != index:
            index = nxt
            continue
        char = text[index]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _parse_params(text: str) -> tuple[list[Param], str | None]:
    """Read the declarations as a Python parameter list, which is exactly what they are.

    Handing the text to Python's own parser is what makes the two spellings one case: a
    declaration per line, as this project's docs show, and the comma-separated one-liner with
    defaults and untyped names that JinjaX writes. Anything Python would reject is reported
    rather than half-read.

    The retry behind a bare ``*`` accepts a defaulted declaration before an undefaulted one,
    which a template may write in whatever order reads best because every generated parameter
    is keyword-only.
    """
    if not text:
        return [], None
    function = _parsed_function(text)
    if function is None:
        return [], f'malformed {{#def #}} header: {text!r} is not a valid parameter list'
    return _params_of(function.args), None


def _parsed_function(text: str, returns: str | None = None) -> ast.FunctionDef | None:
    tree = _parse_signature(text, returns) or (_parse_signature(f'*, {text}', returns) if text else None)
    if tree is None:
        return None
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef):  # pragma: no cover
        return None
    return function


def _parse_signature(text: str, returns: str | None = None) -> ast.Module | None:
    suffix = f' -> {returns}' if returns is not None else ''
    try:
        return ast.parse(f'def _tj({text}){suffix}: pass')
    except SyntaxError:
        return None


def _params_of(args: ast.arguments) -> list[Param]:
    """Flatten Python's argument groups, keeping each name paired with its own default."""
    positional = [*args.posonlyargs, *args.args]
    padding: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    pairs = list(zip(positional, [*padding, *args.defaults], strict=True))
    pairs.extend(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    return [
        Param(
            name=arg.arg,
            annotation=ast.unparse(arg.annotation) if arg.annotation is not None else None,
            default=ast.unparse(default) if default is not None else None,
        )
        for arg, default in pairs
    ]


def header_errors(header: TemplateHeader) -> list[str]:
    """Return messages for header entries that are not valid Python, empty when the header is sound.

    A formatter that collapses the header onto one line produces text that still matches the
    comment pattern but no longer parses as a declaration list, which this catches.
    """
    messages = [
        f'malformed import in {{#def #}} header: {statement!r} (one import per line)'
        for statement in header.imports
        if not _parses(statement)
    ]
    if header.malformed is not None:
        messages.append(header.malformed)
    return messages


def _parses(source: str, mode: str = 'exec') -> bool:
    try:
        ast.parse(source, mode=mode)
    except SyntaxError:
        return False
    return True
