"""Transpiler resilience on real-world Jinja constructs (no pyright needed)."""

import ast

from types_for_jinja.header import parse_header
from types_for_jinja.transpile import transpile


def _code(source, template_path=None):
    header = parse_header(source)
    assert header is not None
    return transpile(source, header, template_path=template_path).code


def _signature(code, name):
    found = [line for line in code.splitlines() if f'def {name}(' in line]
    assert len(found) == 1
    return found[0]


def _imported(tmp_path, directive):
    (tmp_path / 'lib.html').write_text(
        '{% macro wrap() %}\n{#def\ncaller(user: User)\n#}\n{{ caller(user) }}\n{% endmacro %}\n'
    )
    source = '{#def x: int #}\n' + directive + '\n'
    page = tmp_path / 'page.html'
    page.write_text(source)
    return _code(source, page)


def test_macro_is_modeled_with_params():
    src = '{#def x: int #}\n{% macro card(title) %}{{ title }}{% endmacro %}\n{{ card(x) }}'

    code = _code(src)

    assert 'def _render(x: int)' in code
    assert 'def card(title)' in code


def test_with_block_binds_local():
    code = _code('{#def x: int #}\n{% with y = x %}{{ y }}{% endwith %}')

    assert 'y = x' in code


def test_extends_and_block_do_not_crash():
    code = _code('{#def x: int #}\n{% extends "base.html" %}{% block body %}{{ x }}{% endblock %}')

    assert '_ = x' in code


def test_include_does_not_crash():
    assert '_render' in _code('{#def x: int #}\n{% include "other.html" %}{{ x }}')


def test_known_filter_uses_its_declared_signature():
    code = _code('{#def items: list[int] #}\n{{ items | length }}')

    assert 'from ._tj_filters import _tj_f_length' in code
    assert '_tj_f_length(items)' in code


def test_unknown_filter_still_falls_back_to_any():
    code = _code('{#def items: list[int] #}\n{{ items | my_custom_filter }}')

    assert '_tj_any(items)' in code
    assert '_tj_filters' not in code


def test_test_expression_uses_the_boolean_signature():
    code = _code('{#def x: int #}\n{% if x is divisibleby 3 %}y{% endif %}')

    assert 'from ._tj_filters import _tj_test' in code
    assert '_tj_test(x, 3)' in code


def test_slice_is_emitted():
    assert 's[:4]' in _code('{#def s: str #}\n{{ s[:4] }}')


def test_for_else_with_only_literal_text_is_valid_python():
    code = _code('{#def items: list[str] #}\n{% for item in items %}{{ item }}{% else %}text{% endfor %}')

    ast.parse(code)


def test_an_empty_caller_clause_is_a_keyword_only_parameter():
    code = _code(
        '{#def x: int #}\n'
        '{% macro plain(title) %}{{ title }}{% endmacro %}\n'
        '{% macro card() %}\n'
        '{#def\ncaller()\n#}\n'
        '{{ caller() }}\n'
        '{% endmacro %}\n'
    )

    assert '*, caller: _TJCallable[[], _TJAny]) -> _TJAny:' in _signature(code, 'card')
    assert 'caller' not in _signature(code, 'plain')
    assert 'from collections.abc import Callable as _TJCallable' in code
    untouched = _code('{#def x: int #}\n{% macro plain(title) %}{{ title }}{% endmacro %}\n')
    assert '_TJCallable' not in untouched


def test_a_typed_caller_slot_is_a_keyword_only_parameter():
    code = _code(
        '{#def x: int #}\n{% macro card() %}\n{#def\ncaller(user: User)\n#}\n{{ caller(user) }}\n{% endmacro %}\n'
    )

    assert '*, caller: _TJCallable[[User], _TJAny]) -> _TJAny:' in _signature(code, 'card')


def test_a_caller_return_annotation_types_the_callable():
    code = _code(
        '{#def x: int #}\n'
        '{% macro card() %}\n'
        '{#def\ncaller(user: User) -> Markup\n#}\n'
        '{{ caller(user) }}\n'
        '{% endmacro %}\n'
    )

    assert '*, caller: _TJCallable[[User], Markup]) -> _TJAny:' in _signature(code, 'card')


def test_a_bare_caller_slot_is_any():
    code = _code('{#def x: int #}\n{% macro card() %}\n{#def\ncaller(user)\n#}\n{{ caller(user) }}\n{% endmacro %}\n')

    assert '*, caller: _TJCallable[[_TJAny], _TJAny]) -> _TJAny:' in _signature(code, 'card')


def test_caller_is_defined_makes_the_parameter_optional():
    code = _code(
        '{#def x: int #}\n'
        '{% macro card() %}\n'
        '{#def\ncaller()\n#}\n'
        '{% if caller is defined %}{{ caller() }}{% endif %}\n'
        '{% endmacro %}\n'
    )

    assert '*, caller: _TJCallable[[], _TJAny] | None = None' in _signature(code, 'card')


def test_caller_is_undefined_makes_the_parameter_optional():
    code = _code(
        '{#def x: int #}\n'
        '{% macro card() %}\n'
        '{#def\ncaller()\n#}\n'
        '{% if caller is undefined %}hidden{% endif %}\n'
        '{% endmacro %}\n'
    )

    assert '*, caller: _TJCallable[[], _TJAny] | None = None' in _signature(code, 'card')


def test_from_import_copies_the_caller_clause(tmp_path):
    code = _imported(tmp_path, '{% from "lib.html" import wrap %}')

    signature = _signature(code, 'wrap')
    assert '*, caller: _TJCallable[[User], _TJAny]) -> _TJAny:' in signature
    assert signature.startswith('def wrap(')
    assert 'from collections.abc import Callable as _TJCallable' in code


def test_import_copies_the_caller_clause(tmp_path):
    code = _imported(tmp_path, '{% import "lib.html" as ns %}')

    signature = _signature(code, 'wrap')
    assert '*, caller: _TJCallable[[User], _TJAny]) -> _TJAny:' in signature
    assert signature.startswith('    def wrap(')
    assert 'from collections.abc import Callable as _TJCallable' in code
