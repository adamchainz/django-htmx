"""
Random, valid migration histories for one to three apps, from a seed.

The generator keeps its own model of the schema, and only emits operations
that Django can apply to it: nothing names a field that is gone, no model is
deleted while a foreign key points at it, and every column has a default or
allows NULL, so the few rows that data migrations create never get in the way.
Names come from small pools, so they are reused once free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from random import Random
from typing import Any

from tests.project import Project

APPS = ["shop", "lib", "blog"]
MODEL_NAMES = ["Alpha", "Beta", "Gamma", "Delta"]
FIELD_NAMES = ["a", "b", "c", "d", "e", "f"]
OBJECT_NAMES = [f"obj_{n}" for n in "klmnop"]


@dataclass
class Model:
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    indexes: dict[str, str] = field(default_factory=dict)
    """Index name -> the field it covers."""
    constraints: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    """Constraint name -> (kind, field, whether it has a message)."""
    together: set[tuple[str, str]] = field(default_factory=set)
    ordering: str | None = None
    ordered_by: str | None = None
    """The foreign key of order_with_respect_to."""
    has_row: bool = False

    def auto_named(self) -> set[str]:
        """The fields with an index or constraint that Django names."""
        names = {name for pair in self.together for name in pair}
        return names | {
            n
            for n, s in self.fields.items()
            if s["kind"] == "fk"
            or s["options"].get("db_index")
            or s["options"].get("unique")
        }

    def named(self) -> set[str]:
        """The fields that something of the model names."""
        names = set(self.indexes.values()) | {
            f for _, f, _ in self.constraints.values()
        }
        names |= {name for pair in self.together for name in pair}
        return names | {n for n in (self.ordering, self.ordered_by) if n}


def render_field(spec: dict[str, Any]) -> str:
    kind = spec["kind"]
    if kind == "fk":
        return f'models.ForeignKey("{spec["to"]}", models.CASCADE, null=True)'
    options = dict(spec["options"])
    arguments = ", ".join(f"{k}={v!r}" for k, v in options.items())
    cls = {
        "int": "IntegerField",
        "big": "BigIntegerField",
        "char": "CharField",
        "bool": "BooleanField",
        "text": "TextField",
    }[kind]
    return f"models.{cls}({arguments})"


class Generator:
    def __init__(self, seed: int, *, renames_keep_names: bool = False) -> None:
        self.random = Random(seed)
        self.renames_keep_names = renames_keep_names
        """Only rename what has no index or constraint that Django names
        after it: on SQLite, a later table rebuild renames those, so their
        names depend on rebuilds that compacting may remove. A static plan
        can't see that (a trial can)."""
        self.apps = APPS[: self.random.randint(1, 3)]
        self.every: dict[str, dict[str, Model]] = {app: {} for app in self.apps}
        self.app = self.apps[0]
        """The app of the migration being written."""
        self.models = self.every[self.app]
        """Its models, by name."""
        self.last: dict[str, str] = {}
        self.functions: list[str] = []
        self.counter = 0
        self.fresh: set[str] = set()
        """Indexes created in the current migration: Django creates them at
        its end, so the migration can't remove or rename them."""
        self.fresh_together: list[Model] = []
        """Likewise, models whose unique_together the migration set."""

    # Choosing

    def labels(self) -> list[str]:
        """Every model of every app, as "app.Name"."""
        return [
            f"{app}.{name}" for app, models in self.every.items() for name in models
        ]

    def pick(self, items: Any) -> Any:
        return self.random.choice(sorted(items))

    def new_field(self, *, relation: bool = True) -> dict[str, Any]:
        kinds = ["int", "int", "char", "char", "bool", "text"]
        if relation and self.labels():
            kinds += ["fk", "fk"]
        kind = self.random.choice(kinds)
        if kind == "fk":
            return {"kind": "fk", "to": self.pick(self.labels())}
        options: dict[str, Any] = {}
        if kind == "int":
            options["default"] = self.random.randint(0, 5)
            if self.random.random() < 0.3:
                options["db_index"] = True
        elif kind == "char":
            options["max_length"] = self.random.choice([10, 20])
            options["default"] = self.random.choice(["", "x"])
            if self.random.random() < 0.2:
                options["unique"] = True
        elif kind == "bool":
            options["default"] = self.random.choice([True, False])
        else:
            options["null"] = True
        return {"kind": kind, "options": options}

    def free_object_name(self) -> str | None:
        used = {
            n
            for models in self.every.values()
            for m in models.values()
            for n in [*m.indexes, *m.constraints]
        }
        free = sorted(set(OBJECT_NAMES) - used)
        return self.random.choice(free) if free else None

    def referenced(self, name: str) -> bool:
        return any(
            spec.get("to") == f"{self.app}.{name}"
            for models in self.every.values()
            for other in models.values()
            for spec in other.fields.values()
        )

    # Operations: each returns source, or None when it doesn't apply

    def create_model(self) -> str | None:
        free = sorted(set(MODEL_NAMES) - set(self.models))
        if not free:
            return None
        name = self.random.choice(free)
        model = Model()
        for field_name in self.random.sample(FIELD_NAMES, self.random.randint(0, 3)):
            model.fields[field_name] = self.new_field()
        options: list[str] = []
        plain = sorted(n for n, s in model.fields.items() if s["kind"] != "fk")
        if plain and self.random.random() < 0.3:
            index = self.free_object_name()
            if index:
                model.indexes[index] = self.random.choice(plain)
                self.fresh.add(index)
                options.append(
                    f'"indexes": [models.Index(fields=["{model.indexes[index]}"],'
                    f' name="{index}")]'
                )
        if len(model.fields) >= 2 and self.random.random() < 0.2:
            pair = tuple(sorted(self.random.sample(sorted(model.fields), 2)))
            model.together.add(pair)  # type: ignore[arg-type]
            self.fresh_together.append(model)
            options.append(f'"unique_together": {{{pair!r}}}')
        if plain and self.random.random() < 0.2:
            model.ordering = self.random.choice(plain)
            options.append(f'"ordering": ["{model.ordering}"]')
        fields = "".join(
            f'("{n}", {render_field(s)}), ' for n, s in model.fields.items()
        )
        self.models[name] = model
        return (
            f'migrations.CreateModel(name="{name}", fields=[("id",'
            f" models.AutoField(primary_key=True)), {fields}],"
            f" options={{{', '.join(options)}}})"
        )

    def delete_model(self) -> str | None:
        candidates = [n for n in self.models if not self.referenced(n)]
        if not candidates:
            return None
        name = self.pick(candidates)
        del self.models[name]
        return f'migrations.DeleteModel("{name}")'

    def rename_model(self) -> str | None:
        free = sorted(set(MODEL_NAMES) - set(self.models))
        candidates = [
            n
            for n, m in self.models.items()
            if not (self.renames_keep_names and m.auto_named())
        ]
        if not free or not candidates:
            return None
        old, new = self.pick(candidates), self.random.choice(free)
        self.models[new] = self.models.pop(old)
        for models in self.every.values():
            for model in models.values():
                for spec in model.fields.values():
                    if spec.get("to") == f"{self.app}.{old}":
                        spec["to"] = f"{self.app}.{new}"
        return f'migrations.RenameModel("{old}", "{new}")'

    def add_field(self) -> str | None:
        candidates = [n for n, m in self.models.items() if len(m.fields) < 6]
        if not candidates:
            return None
        name = self.pick(candidates)
        model = self.models[name]
        field_name = self.pick(set(FIELD_NAMES) - set(model.fields))
        model.fields[field_name] = spec = self.new_field()
        return f'migrations.AddField("{name}", "{field_name}", {render_field(spec)})'

    def remove_field(self) -> str | None:
        candidates = [
            (n, f) for n, m in self.models.items() for f in set(m.fields) - m.named()
        ]
        if not candidates:
            return None
        name, field_name = self.pick(candidates)
        del self.models[name].fields[field_name]
        return f'migrations.RemoveField("{name}", "{field_name}")'

    def alter_field(self) -> str | None:
        candidates = [(n, f) for n, m in self.models.items() for f in m.fields]
        if not candidates:
            return None
        name, field_name = self.pick(candidates)
        spec = self.models[name].fields[field_name]
        if spec["kind"] == "fk":
            spec["to"] = self.pick(self.labels())
        else:
            options = spec["options"]
            change = self.random.choice(
                ["default", "index", "length", "type", "unique"]
            )
            if change == "default" and "default" in options:
                options["default"] = {
                    "int": self.random.randint(0, 5),
                    "big": self.random.randint(0, 5),
                    "char": self.random.choice(["", "x", "y"]),
                    "bool": self.random.choice([True, False]),
                }[spec["kind"]]
            elif (
                change == "index"
                and spec["kind"] in ("int", "big")
                and not options.pop("db_index", False)
            ):
                options["db_index"] = True
            elif change == "length" and spec["kind"] == "char":
                options["max_length"] += 10
            elif change == "type" and spec["kind"] == "int":
                spec["kind"] = "big"
            elif (
                change == "unique"
                and spec["kind"] == "char"
                and not options.pop("unique", False)
            ):
                options["unique"] = True
        return f'migrations.AlterField("{name}", "{field_name}", {render_field(spec)})'

    def rename_field(self) -> str | None:
        candidates = [
            (n, f)
            for n, m in self.models.items()
            if len(m.fields) < 6
            for f in set(m.fields) - m.named()
            if not (self.renames_keep_names and f in m.auto_named())
        ]
        if not candidates:
            return None
        name, old = self.pick(candidates)
        model = self.models[name]
        new = self.pick(set(FIELD_NAMES) - set(model.fields))
        model.fields = {new if n == old else n: s for n, s in model.fields.items()}
        return f'migrations.RenameField("{name}", "{old}", "{new}")'

    def add_index(self) -> str | None:
        candidates = [(n, f) for n, m in self.models.items() for f in m.fields]
        index = self.free_object_name()
        if not candidates or index is None:
            return None
        name, field_name = self.pick(candidates)
        self.models[name].indexes[index] = field_name
        self.fresh.add(index)
        return (
            f'migrations.AddIndex("{name}", models.Index(fields=["{field_name}"],'
            f' name="{index}"))'
        )

    def remove_index(self) -> str | None:
        candidates = [
            (n, i)
            for n, m in self.models.items()
            for i in m.indexes
            if i not in self.fresh
        ]
        if not candidates:
            return None
        name, index = self.pick(candidates)
        del self.models[name].indexes[index]
        return f'migrations.RemoveIndex("{name}", "{index}")'

    def rename_index(self) -> str | None:
        candidates = [
            (n, i)
            for n, m in self.models.items()
            for i in m.indexes
            if i not in self.fresh
        ]
        new = self.free_object_name()
        if not candidates or new is None:
            return None
        name, old = self.pick(candidates)
        indexes = self.models[name].indexes
        indexes[new] = indexes.pop(old)
        self.fresh.add(new)
        return f'migrations.RenameIndex("{name}", old_name="{old}", new_name="{new}")'

    def render_constraint(self, name: str, spec: tuple[str, str, bool]) -> str:
        kind, field_name, message = spec
        extra = ', violation_error_message="No"' if message else ""
        if kind == "check":
            return (
                f"models.CheckConstraint(condition=models.Q({field_name}__gte=0),"
                f' name="{name}"{extra})'
            )
        return f'models.UniqueConstraint(fields=["{field_name}"], name="{name}"{extra})'

    def add_constraint(self) -> str | None:
        candidates = [
            (n, f, "check" if s["kind"] in ("int", "big") else "unique")
            for n, m in self.models.items()
            for f, s in m.fields.items()
            if s["kind"] in ("int", "big", "char")
        ]
        constraint = self.free_object_name()
        if not candidates or constraint is None:
            return None
        name, field_name, kind = self.pick(candidates)
        spec = (kind, field_name, False)
        self.models[name].constraints[constraint] = spec
        return (
            f'migrations.AddConstraint("{name}",'
            f" {self.render_constraint(constraint, spec)})"
        )

    def alter_constraint(self) -> str | None:
        candidates = [(n, c) for n, m in self.models.items() for c in m.constraints]
        if not candidates:
            return None
        name, constraint = self.pick(candidates)
        kind, field_name, message = self.models[name].constraints[constraint]
        spec = (kind, field_name, not message)
        self.models[name].constraints[constraint] = spec
        return (
            f'migrations.AlterConstraint("{name}", "{constraint}",'
            f" {self.render_constraint(constraint, spec)})"
        )

    def remove_constraint(self) -> str | None:
        candidates = [(n, c) for n, m in self.models.items() for c in m.constraints]
        if not candidates:
            return None
        name, constraint = self.pick(candidates)
        del self.models[name].constraints[constraint]
        return f'migrations.RemoveConstraint("{name}", "{constraint}")'

    def alter_together(self) -> str | None:
        candidates = [
            n
            for n, m in self.models.items()
            if not any(m is fresh for fresh in self.fresh_together)
        ]
        if not candidates:
            return None
        name = self.pick(candidates)
        model = self.models[name]
        model.together = set()
        self.fresh_together.append(model)
        if len(model.fields) >= 2 and self.random.random() < 0.7:
            pair = tuple(sorted(self.random.sample(sorted(model.fields), 2)))
            model.together.add(pair)  # type: ignore[arg-type]
        value = "{" + ", ".join(repr(p) for p in sorted(model.together)) + "}"
        return (
            f'migrations.AlterUniqueTogether("{name}",'
            f" {value if model.together else 'set()'})"
        )

    def alter_options(self) -> str | None:
        if not self.models:
            return None
        name = self.pick(self.models)
        model = self.models[name]
        plain = sorted(n for n, s in model.fields.items() if s["kind"] != "fk")
        model.ordering = (
            self.random.choice(plain) if plain and self.random.random() < 0.6 else None
        )
        options = f'{{"ordering": ["{model.ordering}"]}}' if model.ordering else "{}"
        return f'migrations.AlterModelOptions("{name}", {options})'

    def alter_order(self) -> str | None:
        candidates = [
            (n, f)
            for n, m in self.models.items()
            if "_order" not in m.fields
            for f, s in m.fields.items()
            if s["kind"] == "fk"
        ]
        ordered = [n for n, m in self.models.items() if m.ordered_by]
        if ordered and self.random.random() < 0.5:
            name = self.pick(ordered)
            self.models[name].ordered_by = None
            return f'migrations.AlterOrderWithRespectTo("{name}", None)'
        if not candidates:
            return None
        name, field_name = self.pick(candidates)
        self.models[name].ordered_by = field_name
        return f'migrations.AlterOrderWithRespectTo("{name}", "{field_name}")'

    def mention(self) -> str | None:
        name = self.random.choice([*MODEL_NAMES, *FIELD_NAMES, *OBJECT_NAMES])
        return f'migrations.RunSQL("SELECT 1 /* {name.lower()} */", "SELECT 1")'

    def read_rows(self) -> str | None:
        candidates = [(n, f) for n, m in self.models.items() for f in m.fields]
        if not candidates:
            return None
        name, field_name = self.pick(candidates)
        return self.run_python(
            f'apps.get_model("{self.app}", "{name}").objects.using(alias)'
            f".exclude({field_name}=None).count()"
        )

    def write_row(self) -> str | None:
        candidates = [n for n, m in self.models.items() if not m.has_row]
        if not candidates:
            return None
        name = self.pick(candidates)
        self.models[name].has_row = True
        return self.run_python(
            f'apps.get_model("{self.app}", "{name}").objects.using(alias).create()'
        )

    def run_python(self, body: str) -> str:
        self.counter += 1
        function = f"forwards_{self.counter}"
        self.functions.append(
            f"def {function}(apps, schema_editor):\n"
            "    alias = schema_editor.connection.alias\n"
            f"    {body}\n"
        )
        return f"migrations.RunPython({function}, migrations.RunPython.noop)"

    # The history

    def operation(self) -> str | None:
        choices = [
            (self.create_model, 3),
            (self.delete_model, 1),
            (self.rename_model, 1),
            (self.add_field, 6),
            (self.remove_field, 4),
            (self.alter_field, 6),
            (self.rename_field, 2),
            (self.add_index, 3),
            (self.remove_index, 2),
            (self.rename_index, 2),
            (self.add_constraint, 2),
            (self.alter_constraint, 1),
            (self.remove_constraint, 2),
            (self.alter_together, 2),
            (self.alter_options, 2),
            (self.alter_order, 1),
            (self.mention, 1),
            (self.read_rows, 1),
            (self.write_row, 1),
        ]
        make = self.random.choices(
            [c for c, _ in choices], weights=[w for _, w in choices]
        )[0]
        return make()

    def write(self, project: Project) -> None:
        for number in range(1, self.random.randint(4, 20) + 1):
            self.app = self.random.choice(self.apps)
            self.models = self.every[self.app]
            operations: list[str] = []
            self.functions = []
            self.fresh = set()
            self.fresh_together = []
            if not self.models:
                created = self.create_model()
                assert created is not None
                operations.append(created)
            for _ in range(self.random.randint(1, 4)):
                source = self.operation()
                if source is not None:
                    operations.append(source)
            name = f"{number:04}_step"
            project.migration(
                self.app,
                name,
                "".join(f"{op},\n" for op in operations),
                # After everything written so far: the generator's model of
                # the schema follows the order it writes migrations in.
                extra_deps=[(a, n) for a, n in self.last.items() if a != self.app],
                extra="\n\n".join(self.functions),
            )
            self.last[self.app] = name
