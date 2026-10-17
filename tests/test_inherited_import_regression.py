from pathlib import Path
from textwrap import dedent

import pytest

from types_for_jinja.header import parse_header
from types_for_jinja.transpile import transpile


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


def test_child_assignment_visible_to_parent(tmp_path):
    """Top-level `{% set %}`s should be visible to a parent template."""
    (tmp_path / 'base.html').write_text('{{ page_title }}')

    source = dedent(
        """{#def
        user_id: int
        #}
        {% extends "base.html" %}
        {% set page_title = "Hello" %}
        """
    )
    header = parse_header(source)
    assert header is not None

    generated = transpile(source, header, template_path=tmp_path / 'child.html')

    assert generated.code.index('page_title = ') < generated.code.index('_ = page_title')
