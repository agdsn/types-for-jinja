import ast
from pathlib import Path
from textwrap import dedent

import pytest

from types_for_jinja.config import Config
from types_for_jinja.header import parse_header
from types_for_jinja.layout import layout
from types_for_jinja.transpile import transpile

from .backends import STUB_DIR
from .checked import DEFAULT_BACKEND, Reported, reported, write_template
from .configuration import requires_checker


@pytest.mark.parametrize(
    ('directive', 'call', 'expected'),
    [
        (
            '{% import "macros.html" as navigation with context %}',
            '{{ navigation.link("hello") }}',
            'navigation = _TJNS_navigation',
        ),
        (
            '{% from "macros.html" import link with context %}',
            '{{ link("hello") }}',
            'def link(',
        ),
        (
            '{% if true %}{% import "macros.html" as navigation %}{{ navigation.link("hello") }}{% endif %}',
            '',
            'navigation = _TJNS_navigation',
        ),
        (
            '{% macro show(text) %}{% import "macros.html" as navigation %}{{ navigation.link(text) }}{% endmacro %}',
            '{{ show("hello") }}',
            'navigation = _TJNS_navigation',
        ),
    ],
)
def test_inherited_macro_import(
    tmp_path: Path,
    directive: str,
    call: str,
    expected: str,
):
    (tmp_path / 'macros.html').write_text('{% macro link(text) %}{{ text }}{% endmacro %}')
    (tmp_path / 'base.html').write_text(f'{directive}\n{call}\n{{% block content %}}{{% endblock %}}')

    source = dedent(
        """{#def
        title: str
        #}
        {% extends "base.html" %}
        {% block content %}{{ title }}{% endblock %}
        """
    )
    child = tmp_path / 'child.html'
    child.write_text(source)

    header = parse_header(source)
    assert header is not None

    generated = transpile(source, header, template_path=child)

    assert expected in generated.code


_IMPORT_SCOPES = [
    (
        dedent("""\
            {% import "lib.html" as ns %}
            {{ ns.f(x) }}
            """),
        'ns = _TJNS_ns',
    ),
    (
        dedent("""\
            {% block body %}
            {% import "lib.html" as ns %}
            {{ ns.f(x) }}
            {% endblock %}
            """),
        'ns = _TJNS_ns',
    ),
    (
        dedent("""\
            {% if x %}
            {% import "lib.html" as ns %}
            {{ ns.f(x) }}
            {% endif %}
            """),
        'ns = _TJNS_ns',
    ),
    (
        dedent("""\
            {% macro g(x) %}
            {% import "lib.html" as ns %}
            {{ ns.f(x) }}
            {% endmacro %}
            {{ g(x) }}
            """),
        'ns = _TJNS_ns',
    ),
    (
        dedent("""\
            {% extends "base.html" %}
            {% block body %}
            {% import "lib.html" as ns %}
            {{ ns.f(x) }}
            {% endblock %}
            """),
        'ns = _TJNS_ns',
    ),
    (
        dedent("""\
            {% block body %}
            {% from "lib.html" import f %}
            {{ f(x) }}
            {% endblock %}
            """),
        'def f(',
    ),
]


@pytest.mark.parametrize(('body', 'binding'), _IMPORT_SCOPES)
def test_imported_macro_is_bound_in_any_scope(tmp_path: Path, body: str, binding: str):
    """``{% import %}`` and ``{% from %}`` bind their names inside a block, branch, or macro.

    A direct child of the template already became a class or a def. Nested, the stub kept
    only the call and the checker reported the alias as undefined.
    """
    (tmp_path / 'lib.html').write_text('{% macro f(value) %}{{ value }}{% endmacro %}\n')
    (tmp_path / 'base.html').write_text('{% block body %}{% endblock %}\n')
    source = '{#def\nx: str\n#}\n' + body
    page = tmp_path / 'page.html'
    page.write_text(source)
    header = parse_header(source)
    assert header is not None

    generated = transpile(source, header, template_path=page)

    assert generated.code.count(binding) == 1
    assert 'f(' in generated.code


_MACRO = """{% macro f(value) %}
{#def
value: str
#}
{{ value }}
{% endmacro %}
"""

_CALL = """{#def
x: str
#}
{% block body %}
{% import "lib.html" as ns %}
{{ ns.f(x) }}
{% endblock %}
"""


def _check_import(project: Path, call: str) -> list[Reported]:
    (project / 'templates' / 'lib.html').write_text(_MACRO)
    template = write_template('templates/page.html', _CALL.replace('ns.f(x)', call))
    return reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR))


@requires_checker(DEFAULT_BACKEND)
def test_import_inside_a_block_typechecks(project: Path):
    assert _check_import(project, 'ns.f(x)') == []


@requires_checker(DEFAULT_BACKEND)
def test_import_inside_a_block_still_checks_the_argument(project: Path):
    """A bad argument is a type error on the call."""
    found = _check_import(project, 'ns.f(1)')

    assert found
    assert {entry.line for entry in found} == {6}
    assert not any(entry.mentions('not defined') for entry in found)


_LIB = """{% macro f(value) %}
{#def
value: str
#}
{{ value }}
{% endmacro %}
"""

_INCLUDED_IMPORTS = [
    '{% import "lib.html" as ns %}\n{{ ns.f(x) }}\n',
    '{% macro g(value) %}{% import "lib.html" as ns %}{{ ns.f(value) }}{% endmacro %}\n{{ g(x) }}\n',
    '{#def\nx: str\n#}\n{% import "lib.html" as ns %}\n{{ ns.f(x) }}\n',
]


@pytest.mark.parametrize('included', _INCLUDED_IMPORTS)
def test_include_binds_an_import_from_the_included_template(tmp_path: Path, included: str):
    """``{% include %}`` copies the other template's body, so its ``{% import %}`` has to bind too."""
    (tmp_path / 'lib.html').write_text(_LIB)
    (tmp_path / 'a1.html').write_text(included)
    source = '{#def\nx: str\n#}\n{% include "a1.html" %}\n'
    page = tmp_path / 'a.html'
    page.write_text(source)
    header = parse_header(source)
    assert header is not None

    generated = transpile(source, header, template_path=page)

    assert generated.code.count('ns = _TJNS_ns') == 1
    assert 'ns.f(' in generated.code


def _check_include(project: Path, call: str) -> list[Reported]:
    (project / 'templates' / 'lib.html').write_text(_LIB)
    (project / 'templates' / 'a1.html').write_text(f'{{% import "lib.html" as ns %}}\n{{{{ {call} }}}}\n')
    template = write_template('templates/a.html', '{#def\nx: str\n#}\n{% include "a1.html" %}\n')
    return reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR))


@requires_checker(DEFAULT_BACKEND)
def test_include_of_an_import_typechecks(project: Path):
    assert _check_include(project, 'ns.f(x)') == []


@requires_checker(DEFAULT_BACKEND)
def test_include_of_an_import_still_checks_the_argument(project: Path):
    """A bad argument is a type error on the include, where the other file's lines are reported."""
    found = _check_include(project, 'ns.f(1)')

    assert found
    assert {entry.line for entry in found} == {4}
    assert not any(entry.mentions('not defined') for entry in found)


@pytest.mark.parametrize('set_first', [False, True])
def test_child_assignment_visible_to_parent(tmp_path: Path, *, set_first: bool):
    """Top-level `{% set %}`s should be visible to a parent template, on either side of extends."""
    (tmp_path / 'base.html').write_text('{{ page_title }}')
    extends = '{% extends "base.html" %}'
    assignment = '{% set page_title = "Hello" %}'
    body = f'{assignment}\n{extends}' if set_first else f'{extends}\n{assignment}'
    source = f'{{#def\nuser_id: int\n#}}\n{body}\n'
    header = parse_header(source)
    assert header is not None

    generated = transpile(source, header, template_path=tmp_path / 'child.html')

    assert generated.code.index('page_title = ') < generated.code.index('_ = page_title')
    aligned = layout(generated, header, 'child_tj_shared')
    assert aligned is not None
    # The parent is checked from the sidecar, which has to keep the literal so the type is str.
    assert aligned.sidecar.index("page_title = 'Hello'") < aligned.sidecar.index('_ = page_title')
    assert 'page_title: _TJAny' not in aligned.sidecar
    assignment_line = source.splitlines().index(assignment)
    assert aligned.code.splitlines()[assignment_line] == "page_title = 'Hello'"


@pytest.mark.parametrize('set_first', [False, True])
@requires_checker('mypy')
def test_parent_sees_the_assigned_type(project: Path, *, set_first: bool):
    """A parent expression is checked against the inferred type, so a bad use is still an error."""
    extends = '{% extends "base.html" %}'
    assignment = '{% set page_title = "Hello" %}'
    body = f'{assignment}\n{extends}' if set_first else f'{extends}\n{assignment}'
    child = f'{{#def\nuser_id: int\n#}}\n{body}\n'
    (project / 'templates' / 'base.html').write_text('{{ page_title + 1 }}\n', encoding='utf-8')
    template = write_template('templates/child.html', child)

    found = reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR), backend='mypy')

    extends_line = child.splitlines().index(extends) + 1
    assert found
    assert all(entry.line == extends_line for entry in found)
    assert any('str' in entry.message and 'int' in entry.message for entry in found)
    assert not any(entry.mentions('not defined') for entry in found)


@pytest.mark.parametrize('set_first', [False, True])
@requires_checker('mypy')
def test_parent_accepts_a_well_typed_child_assignment(project: Path, *, set_first: bool):
    """A parent may read the assigned name once the child has given it a type."""
    extends = '{% extends "base.html" %}'
    assignment = '{% set page_title = "Hello" %}'
    body = f'{assignment}\n{extends}' if set_first else f'{extends}\n{assignment}'
    child = f'{{#def\nuser_id: int\n#}}\n{body}\n'
    (project / 'templates' / 'base.html').write_text('{{ page_title }}\n', encoding='utf-8')
    template = write_template('templates/child.html', child)

    assert reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR), backend='mypy') == []


_TYPED_FRAGMENT = """{#def
x: str
#}
{% import "lib.html" as misc %}
{{ misc.f(x) }}
"""


def _assignment_inside_function(code: str, name: str) -> bool:
    """Whether ``name = ...`` appears inside a function body, not at module level."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in node.body:
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            return True
    return False


def _module_level_names(code: str) -> set[str]:
    tree = ast.parse(code)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
    return names


def _aligned_include(tmp_path: Path, *fragments: str) -> tuple[str, str]:
    """Transpile a parent that includes each fragment; return aligned code and sidecar."""
    (tmp_path / 'lib.html').write_text(_LIB)
    includes = []
    for index, fragment in enumerate(fragments):
        name = f'a{index}.html'
        (tmp_path / name).write_text(fragment)
        includes.append(f'{{% include "{name}" %}}')
    source = '{#def\nx: str\n#}\n' + '\n'.join(includes) + '\n'
    page = tmp_path / 'page.html'
    page.write_text(source)
    header = parse_header(source)
    assert header is not None
    generated = transpile(source, header, template_path=page)
    aligned = layout(generated, header, 'page_tj_shared')
    assert aligned is not None
    return aligned.code, aligned.sidecar


def test_two_typed_includes_bind_the_same_import_inside_each_function(tmp_path: Path):
    """Each include owns its ``misc``; the parent stub must not assign it at module level."""
    code, sidecar = _aligned_include(tmp_path, _TYPED_FRAGMENT, _TYPED_FRAGMENT)

    assert sidecar.count('misc = _TJNS_misc') == 2
    assert _assignment_inside_function(sidecar, 'misc')
    assert 'misc' not in _module_level_names(sidecar)
    assert 'misc =' not in code
    ast.parse(sidecar)


@requires_checker(DEFAULT_BACKEND)
def test_two_typed_includes_can_import_different_templates_as_misc(project: Path):
    """Two fragments may bind the same alias to different macros; a dedup would collapse them."""
    (project / 'templates' / 'lib1.html').write_text(
        '{% macro f(value) %}\n{#def\nvalue: str\n#}\n{{ value }}\n{% endmacro %}\n'
    )
    (project / 'templates' / 'lib2.html').write_text(
        '{% macro g(value) %}\n{#def\nvalue: str\n#}\n{{ value }}\n{% endmacro %}\n'
    )
    write_template(
        'templates/a0.html',
        '{#def\nx: str\n#}\n{% import "lib1.html" as misc %}\n{{ misc.f(x) }}\n',
    )
    write_template(
        'templates/a1.html',
        '{#def\nx: str\n#}\n{% import "lib2.html" as misc %}\n{{ misc.g(x) }}\n',
    )
    template = write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "a0.html" %}\n{% include "a1.html" %}\n',
    )

    assert reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR)) == []


@requires_checker(DEFAULT_BACKEND)
def test_included_import_is_undefined_in_the_parent(project: Path):
    """Jinja does not export an include's ``{% import %}``; the parent stub must not either."""
    (project / 'templates' / 'lib.html').write_text(_LIB)
    write_template(
        'templates/a0.html',
        '{#def\nx: str\n#}\n{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n',
    )
    template = write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "a0.html" %}\n{{ misc }}\n',
    )

    found = reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR))

    assert found
    assert any(entry.mentions('misc') and entry.mentions('not defined') for entry in found)


def test_two_untyped_includes_with_the_same_import_typecheck(tmp_path: Path):
    """An untyped include parks the same no-arg function so its import is not a sidecar global."""
    fragment = '{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n'
    code, sidecar = _aligned_include(tmp_path, fragment, fragment)

    assert 'def tj_include_a0_html() -> None:' in sidecar
    assert 'def tj_include_a1_html() -> None:' in sidecar
    assert sidecar.count('misc = _TJNS_misc') == 2
    assert _assignment_inside_function(sidecar, 'misc')
    assert 'misc' not in _module_level_names(sidecar)
    assert 'misc =' not in code
    ast.parse(sidecar)


@requires_checker(DEFAULT_BACKEND)
def test_two_untyped_includes_leave_misc_undefined_in_the_parent(project: Path):
    (project / 'templates' / 'lib.html').write_text(_LIB)
    write_template('templates/a0.html', '{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n')
    write_template('templates/a1.html', '{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n')
    template = write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "a0.html" %}\n{% include "a1.html" %}\n{{ misc }}\n',
    )

    found = reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR))

    assert found
    assert any(entry.mentions('misc') and entry.mentions('not defined') for entry in found)
    assert not any(entry.mentions('multiple types') for entry in found)


def test_two_includes_with_the_same_from_import(tmp_path: Path):
    """``{% from %}`` stubs live inside each include function, not as two module-level defs."""
    fragment = '{#def\nx: str\n#}\n{% from "lib.html" import f %}\n{{ f(x) }}\n'
    _, sidecar = _aligned_include(tmp_path, fragment, fragment)

    tree = ast.parse(sidecar)
    module_defs = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert module_defs.count('f') == 0
    nested = [
        child.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        for child in node.body
        if isinstance(child, ast.FunctionDef)
    ]
    assert nested.count('f') == 2


@requires_checker(DEFAULT_BACKEND)
def test_two_includes_with_the_same_from_import_typecheck(project: Path):
    (project / 'templates' / 'lib.html').write_text(_LIB)
    write_template('templates/a0.html', '{#def\nx: str\n#}\n{% from "lib.html" import f %}\n{{ f(x) }}\n')
    write_template('templates/a1.html', '{#def\nx: str\n#}\n{% from "lib.html" import f %}\n{{ f(x) }}\n')
    template = write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "a0.html" %}\n{% include "a1.html" %}\n',
    )

    assert reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR)) == []


@requires_checker(DEFAULT_BACKEND)
def test_nested_include_import_does_not_leak(project: Path):
    """An import in an inner include stays there; the outer template does not see it."""
    (project / 'templates' / 'lib.html').write_text(_LIB)
    write_template(
        'templates/inner.html',
        '{#def\nx: str\n#}\n{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n',
    )
    outer = write_template(
        'templates/outer.html',
        '{#def\nx: str\n#}\n{% include "inner.html" %}\n{{ misc }}\n',
    )
    write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "outer.html" %}\n',
    )

    found = reported(outer, Config(template_dirs=['templates'], out_dir=STUB_DIR))
    assert found
    assert any(entry.mentions('misc') and entry.mentions('not defined') for entry in found)

    assert (
        reported(
            Path('templates/inner.html'),
            Config(template_dirs=['templates'], out_dir=STUB_DIR),
        )
        == []
    )


def test_nested_include_import_lives_under_the_inner_function(tmp_path: Path):
    (tmp_path / 'lib.html').write_text(_LIB)
    (tmp_path / 'inner.html').write_text(
        '{#def\nx: str\n#}\n{% import "lib.html" as misc %}\n{{ misc.f(x) }}\n'
    )
    (tmp_path / 'outer.html').write_text('{#def\nx: str\n#}\n{% include "inner.html" %}\n')
    source = '{#def\nx: str\n#}\n{% include "outer.html" %}\n'
    page = tmp_path / 'page.html'
    page.write_text(source)
    header = parse_header(source)
    assert header is not None
    generated = transpile(source, header, template_path=page)
    aligned = layout(generated, header, 'page_tj_shared')
    assert aligned is not None
    sidecar = aligned.sidecar
    ast.parse(sidecar)

    tree = ast.parse(sidecar)
    by_name = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert 'tj_include_inner_html' in by_name
    assert 'tj_include_outer_html' in by_name
    inner_assigns = [
        target.id
        for child in by_name['tj_include_inner_html'].body
        if isinstance(child, ast.Assign)
        for target in child.targets
        if isinstance(target, ast.Name)
    ]
    outer_assigns = [
        target.id
        for child in by_name['tj_include_outer_html'].body
        if isinstance(child, ast.Assign)
        for target in child.targets
        if isinstance(target, ast.Name)
    ]
    assert 'misc' in inner_assigns
    assert 'misc' not in outer_assigns


@pytest.mark.parametrize(
    'direction',
    [
        'base_imports',
        'child_imports',
    ],
)
@requires_checker(DEFAULT_BACKEND)
def test_extends_import_stays_visible_across_the_chain(project: Path, direction: str):
    """``{% extends %}`` shares context; an import on either side reaches the other."""
    (project / 'templates' / 'lib.html').write_text(_LIB)
    if direction == 'base_imports':
        (project / 'templates' / 'base.html').write_text(
            '{% import "lib.html" as misc %}\n{% block body %}{% endblock %}\n'
        )
        child = '{#def\nx: str\n#}\n{% extends "base.html" %}\n{% block body %}{{ misc.f(x) }}{% endblock %}\n'
    else:
        (project / 'templates' / 'base.html').write_text('{{ misc.f(x) }}\n{% block body %}{% endblock %}\n')
        child = (
            '{#def\nx: str\n#}\n{% import "lib.html" as misc %}\n'
            '{% extends "base.html" %}\n{% block body %}{% endblock %}\n'
        )
    template = write_template('templates/child.html', child)

    assert reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR)) == []


@requires_checker(DEFAULT_BACKEND)
def test_untyped_include_set_does_not_leak(project: Path):
    """Wrapping an untyped include keeps its ``{% set %}`` out of the parent stub."""
    write_template('templates/frag.html', '{% set page_title = "Hello" %}\n')
    template = write_template(
        'templates/page.html',
        '{#def\nx: str\n#}\n{% include "frag.html" %}\n{{ page_title }}\n',
    )

    found = reported(template, Config(template_dirs=['templates'], out_dir=STUB_DIR))

    assert found
    assert any(entry.mentions('page_title') and entry.mentions('not defined') for entry in found)
