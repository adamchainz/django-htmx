from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from django_clean_migrations.history import SourceFile
from django_clean_migrations.rewrite import FileEdit, RewriteError


def remove(source: str, *indexes: int, target: int = 0) -> str:
    """Remove elements from the ``target``-th container assigned to ``x``."""
    source = textwrap.dedent(source)
    sf = SourceFile(Path("m.py"), source)
    assert sf.tree is not None
    values = [
        n.value
        for n in ast.walk(sf.tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "x"
    ]
    fe = FileEdit(sf)
    fe.remove(values[target], set(indexes))
    result = fe.render()
    compile(result, "m.py", "exec")
    return result


def test_single_line_middle() -> None:
    assert remove("x = [a, b, c]\n", 1) == "x = [a, c]\n"


def test_single_line_first() -> None:
    assert remove("x = [a, b, c]\n", 0) == "x = [b, c]\n"


def test_single_line_last() -> None:
    assert remove("x = [a, b, c]\n", 2) == "x = [a, b]\n"


def test_single_line_trailing_comma() -> None:
    assert remove("x = [a, b, c,]\n", 2) == "x = [a, b]\n"


def test_single_line_several() -> None:
    assert remove("x = [a, b, c, d]\n", 0, 2, 3) == "x = [b]\n"


def test_single_line_all() -> None:
    assert remove("x = [a, b]\n", 0, 1) == "x = []\n"


def test_tuple_keeps_comma() -> None:
    assert remove("x = (a, b)\n", 1) == "x = (a,)\n"
    assert remove("x = (a, b)\n", 0) == "x = (b,)\n"
    assert remove("x = (a, b, c)\n", 0) == "x = (b, c)\n"


def test_set_emptied() -> None:
    assert remove("x = {('a', 'b')}\n", 0) == "x = set()\n"
    assert remove("x = {\n    ('a', 'b'),\n}\n", 0) == "x = set()\n"


def test_dict_key() -> None:
    assert remove("x = {'a': 1, 'b': [2, 3]}\n", 1) == "x = {'a': 1}\n"
    assert remove("x = {'a': 1}\n", 0) == "x = {}\n"


def test_multi_line() -> None:
    source = """\
    x = [
        a,
        b(
            1,
            2,
        ),  # trailing comment
        c,
    ]
    """
    assert remove(source, 1) == textwrap.dedent(
        """\
        x = [
            a,
            c,
        ]
        """
    )


def test_multi_line_last_without_comma() -> None:
    source = """\
    x = [
        a,
        b
    ]
    """
    assert remove(source, 1) == "x = [\n    a,\n]\n"


def test_multi_line_tuple_single_left() -> None:
    source = """\
    x = (
        a,
        b
    )
    """
    assert remove(source, 0) == "x = (\n    b,\n)\n"


def test_multi_line_all() -> None:
    source = """\
    x = [
        # header
        a,
        b,
    ]
    """
    assert remove(source, 0, 1) == "x = []\n"


def test_attached_comment() -> None:
    source = """\
    x = [
        a,
        # About b.
        # More about b.
        b,

        # About c, detached.

        c,
        d,
    ]
    """
    assert remove(source, 1, 3) == textwrap.dedent(
        """\
        x = [
            a,

            # About c, detached.

            c,
        ]
        """
    )


def test_comment_after_opener_kept() -> None:
    source = """\
    x = [  # keep me
        a,
        b,
    ]
    """
    assert remove(source, 0) == "x = [  # keep me\n    b,\n]\n"


def test_mixed_lines() -> None:
    source = """\
    x = [
        a, b,
        c,
    ]
    """
    assert remove(source, 1) == "x = [\n    a,\n    c,\n]\n"
    assert remove(source, 0) == "x = [\n    b,\n    c,\n]\n"


def test_mixed_last_on_closer_line() -> None:
    source = """\
    x = [
        a,  # note
        b]
    """
    assert remove(source, 1) == "x = [\n    a,  # note\n]\n"


def test_crlf_preserved() -> None:
    assert remove("x = [\r\n    a,\r\n    b,\r\n]\r\n", 0) == "x = [\r\n    b,\r\n]\r\n"


def test_nested_edits_inside_removed_element() -> None:
    source = "x = [[1, 2], [3]]\n"
    sf = SourceFile(Path("m.py"), source)
    assert sf.tree is not None
    outer = sf.tree.body[0].value  # type: ignore[attr-defined]
    fe = FileEdit(sf)
    fe.remove(outer.elts[0], {0})
    fe.remove(outer, {0})
    assert fe.render() == "x = [[3]]\n"


def test_not_a_container() -> None:
    sf = SourceFile(Path("m.py"), "x = f(a)\n")
    assert sf.tree is not None
    with pytest.raises(RewriteError):
        FileEdit(sf).remove(sf.tree.body[0].value, {0})  # type: ignore[attr-defined]
