from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest
from django.db import migrations, models
from tokenize_rt import src_to_tokens

from django_compact_migrations.history import OpRecord, SourceFile, load_history
from django_compact_migrations.rewrite import (
    FileEdit,
    OpEdit,
    RewriteError,
    _filter_together,
    _missing_imports,
    derive_op_edit,
    edited_operation,
    regeneration_problem,
    rewrite_source,
    same_operation,
    unchanged,
)
from django_compact_migrations.tokens import container_for_node
from tests.project import Project


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


def test_single_line_middle():
    assert remove("x = [a, b, c]\n", 1) == "x = [a, c]\n"


def test_single_line_first():
    assert remove("x = [a, b, c]\n", 0) == "x = [b, c]\n"


def test_single_line_last():
    assert remove("x = [a, b, c]\n", 2) == "x = [a, b]\n"


def test_single_line_trailing_comma():
    assert remove("x = [a, b, c,]\n", 2) == "x = [a, b]\n"


def test_single_line_several():
    assert remove("x = [a, b, c, d]\n", 0, 2, 3) == "x = [b]\n"


def test_single_line_all():
    assert remove("x = [a, b]\n", 0, 1) == "x = []\n"


def test_tuple_keeps_comma():
    assert remove("x = (a, b)\n", 1) == "x = (a,)\n"
    assert remove("x = (a, b)\n", 0) == "x = (b,)\n"
    assert remove("x = (a, b, c)\n", 0) == "x = (b, c)\n"


def test_set_emptied():
    assert remove("x = {('a', 'b')}\n", 0) == "x = set()\n"
    assert remove("x = {\n    ('a', 'b'),\n}\n", 0) == "x = set()\n"


def test_dict_key():
    assert remove("x = {'a': 1, 'b': [2, 3]}\n", 1) == "x = {'a': 1}\n"
    assert remove("x = {'a': 1}\n", 0) == "x = {}\n"


def test_multi_line():
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


def test_multi_line_last_without_comma():
    source = """\
    x = [
        a,
        b
    ]
    """
    assert remove(source, 1) == "x = [\n    a,\n]\n"


def test_multi_line_tuple_single_left():
    source = """\
    x = (
        a,
        b
    )
    """
    assert remove(source, 0) == "x = (\n    b,\n)\n"


def test_multi_line_all():
    source = """\
    x = [
        # header
        a,
        b,
    ]
    """
    assert remove(source, 0, 1) == "x = []\n"


def test_attached_comment():
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


def test_comment_after_opener_kept():
    source = """\
    x = [  # keep me
        a,
        b,
    ]
    """
    assert remove(source, 0) == "x = [  # keep me\n    b,\n]\n"


def test_mixed_lines():
    source = """\
    x = [
        a, b,
        c,
    ]
    """
    assert remove(source, 1) == "x = [\n    a,\n    c,\n]\n"
    assert remove(source, 0) == "x = [\n    b,\n    c,\n]\n"


def test_mixed_last_on_closer_line():
    source = """\
    x = [
        a,  # note
        b]
    """
    assert remove(source, 1) == "x = [\n    a,  # note\n]\n"


def test_crlf_preserved():
    assert remove("x = [\r\n    a,\r\n    b,\r\n]\r\n", 0) == "x = [\r\n    b,\r\n]\r\n"


def test_nested_edits_inside_removed_element():
    source = "x = [[1, 2], [3]]\n"
    sf = SourceFile(Path("m.py"), source)
    assert sf.tree is not None
    outer = sf.tree.body[0].value  # type: ignore[attr-defined]
    fe = FileEdit(sf)
    fe.remove(outer.elts[0], {0})
    fe.remove(outer, {0})
    assert fe.render() == "x = [[3]]\n"


def test_not_a_container():
    sf = SourceFile(Path("m.py"), "x = f(a)\n")
    assert sf.tree is not None
    with pytest.raises(RewriteError):
        FileEdit(sf).remove(sf.tree.body[0].value, {0})  # type: ignore[attr-defined]


def test_element_without_space_after_comma():
    assert remove("x = [a,b]\n", 0) == "x = [b]\n"


def test_trailing_element_after_comment_on_shared_line():
    source = """\
    x = [
        a,
        # about b
        b, c]
    """
    assert remove(source, 1, 2) == "x = [\n    a,\n    # about b\n]\n"


def test_comment_at_line_start_attached():
    source = "x = [\n    a,\n# about b\n    b,\n]\n"
    assert remove(source, 1) == "x = [\n    a,\n]\n"


def test_tuple_single_left_with_comma():
    assert remove("x = (a, b,)\n", 0) == "x = (b,)\n"


# Whole operations


def test_filter_together():
    assert _filter_together({("a", "b"), ("c", "d")}, {("a", "b")}) == {("c", "d")}
    assert _filter_together(("a", "b"), set()) == ("a", "b")
    assert _filter_together([["a", "b"], ["c", "d"]], {("a", "b")}) == [["c", "d"]]
    assert _filter_together((("a", "b"), ("c", "d")), {("c", "d")}) == (("a", "b"),)
    assert _filter_together([("a", "b")], {("a", "b")}) is None


def records(project: Project, operations: str, extra: str = "") -> list[OpRecord]:
    project.migration("shop", "0001_initial", operations, extra=extra)
    project.activate()
    with project.db_blocker.unblock():
        return list(load_history().ops)


CREATE = """
migrations.CreateModel(
    name="Item",
    fields=[
        ("id", models.AutoField(primary_key=True)),
        ("name", models.CharField(max_length=10)),
        ("code", models.CharField(max_length=10)),
    ],
    options={
        "unique_together": [("id", "name"), ("id", "code")],
        "indexes": [models.Index(fields=["id"], name="i")],
    },
),
migrations.AlterUniqueTogether("item", ("id", "name")),
migrations.AddField("item", "qty", models.IntegerField(default=0)),
"""


def test_edited_operation(project):
    create, _, add = records(project, CREATE)
    assert edited_operation(OpEdit(create)) is create.op
    edit = OpEdit(
        create, together={"unique_together": {("id", "name")}, "index_together": set()}
    )
    new = edited_operation(edit)
    assert new.options["unique_together"] == [("id", "code")]  # type: ignore[union-attr]
    with pytest.raises(RewriteError, match="cannot edit AddField"):
        edited_operation(OpEdit(add, fields={"qty"}))


def test_rewrite_source(project):
    create, together, add = records(project, CREATE)
    source = create.migration.source
    assert source is not None
    text = rewrite_source(
        source,
        [
            OpEdit(add),  # nothing to do
            OpEdit(
                create,
                together={"unique_together": {("id", "name")}, "index_together": set()},
            ),
            OpEdit(together, entries={("id", "name")}),
        ],
    )
    assert '"unique_together": [("id", "code")],' in text
    assert 'migrations.AlterUniqueTogether("item", ()),' in text
    unchanged_text = rewrite_source(
        source, [OpEdit(create, together={"unique_together": set()})]
    )
    assert unchanged_text == source.text


def test_rewrite_source_failures(project):
    create, together, add = records(project, CREATE)
    source = create.migration.source
    assert source is not None
    create.migration.ops_container = None
    with pytest.raises(RewriteError, match="are not editable"):
        rewrite_source(source, [OpEdit(add, remove=True)])


def test_rewrite_unlocatable(project):
    create, together = records(
        project,
        """
        migrations.CreateModel(name="Item", fields=FIELDS),
        migrations.AlterUniqueTogether("item", TOGETHER),
        """,
        extra='FIELDS = [("id", models.AutoField(primary_key=True))]\n'
        'TOGETHER = [("id",)]',
    )
    source = create.migration.source
    assert source is not None
    with pytest.raises(RewriteError, match="fields is not a literal"):
        rewrite_source(source, [OpEdit(create, fields={"id"})])
    with pytest.raises(RewriteError, match="not a literal"):
        rewrite_source(source, [OpEdit(together, entries={("id",)})])


def test_regenerate_failures():
    op = migrations.AddField("item", "qty", models.IntegerField(default=0))
    clash = SourceFile(
        Path("m.py"), "from django.db import migrations\nmodels = None\nx = [f()]\n"
    )
    node = clash.tree.body[-1].value.elts[0]  # type: ignore[attr-defined]
    with pytest.raises(RewriteError, match="'models' is already bound"):
        FileEdit(clash).regenerate(node, op)
    bare = SourceFile(Path("m.py"), "x = [f()]\n")
    node = bare.tree.body[0].value.elts[0]  # type: ignore[attr-defined]
    with pytest.raises(RewriteError, match="no top-level imports"):
        FileEdit(bare).regenerate(node, op)
    elsewhere = ast.Call(ast.Name("f"), [], [], lineno=9, col_offset=0)
    elsewhere.end_lineno, elsewhere.end_col_offset = 9, 3
    with pytest.raises(RewriteError, match="cannot find the operation at line 9"):
        FileEdit(bare).regenerate(elsewhere, op)


def test_missing_imports():
    source = SourceFile(Path("m.py"), "os = 1\n")
    assert (
        _missing_imports(source, {"import a as b"})
        == "unsupported import 'import a as b'"
    )
    assert _missing_imports(source, {"from json import dumps"}) == [
        "from json import dumps"
    ]
    assert (
        _missing_imports(source, {"import os"})
        == "'os' is already bound to something else"
    )


class Nowhere:
    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ("nowhere_at_all.Thing", [], {})


def test_regeneration_problem():
    source = SourceFile(Path("m.py"), "from django.db import migrations\n")
    assert regeneration_problem(None, migrations.RunSQL("")) == (
        "the source is not readable"
    )
    unserializable = migrations.RunPython(lambda apps, editor: None)
    assert "can't be serialized" in str(regeneration_problem(source, unserializable))
    unloadable = migrations.RunSQL(Nowhere())  # type: ignore[arg-type]
    assert "doesn't load" in str(regeneration_problem(source, unloadable))


def test_operation_comparisons():
    sql = migrations.RunSQL("")
    add = migrations.AddField("item", "qty", models.IntegerField())
    assert same_operation(sql, add) is False
    assert unchanged(sql, add) is False
    lambda_op = migrations.RunPython(lambda apps, editor: None)
    assert unchanged(lambda_op, lambda_op) is False


def test_derive_op_edit(project):
    create, together, add = records(project, CREATE)
    assert derive_op_edit(create, add.op) is None
    op = create.op
    same = migrations.CreateModel(op.name, op.fields, dict(op.options))  # type: ignore[attr-defined]
    edit = derive_op_edit(create, same)
    assert edit is not None and edit.empty
    indexed = migrations.CreateModel(
        op.name,  # type: ignore[attr-defined]
        op.fields,  # type: ignore[attr-defined]
        {**op.options, "indexes": [models.Index(fields=["name"], name="j")]},  # type: ignore[attr-defined]
    )
    assert derive_op_edit(create, indexed) is None
    widened = migrations.CreateModel(
        op.name,  # type: ignore[attr-defined]
        op.fields,  # type: ignore[attr-defined]
        {"unique_together": [("id", "name"), ("name", "code")]},
    )
    assert derive_op_edit(create, widened) is None
    assert derive_op_edit(together, migrations.AlterUniqueTogether("other", ())) is None
    assert (
        derive_op_edit(together, migrations.AlterUniqueTogether("item", {("a", "b")}))
        is None
    )


def _container(source: str) -> Any:
    tree = ast.parse(source)
    tokens = list(src_to_tokens(source))
    index = {
        (t.line, t.utf8_byte_offset): i
        for i, t in enumerate(tokens)
        if t.line is not None
    }
    node = tree.body[0].value  # type: ignore[attr-defined]
    return container_for_node(tokens, index, node)


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("x = [1, 2]", True),
        ("x = 1, 2", False),  # no brackets to edit
        ("x = (1), 2", False),  # the first bracket isn't the tuple's
        ("x = {**a, 'b': 1}", False),
        ("x = [lambda a, b: 1, 2]", False),  # commas inside an element
    ],
)
def test_containers(source, found):
    assert (_container(source) is not None) is found
