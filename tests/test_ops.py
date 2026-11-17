from __future__ import annotations

from django.contrib.postgres import operations as pg_operations

from django_compact_migrations.ops import INERT, OPAQUE
from django_compact_migrations.ops import classify as classify_op
from tests.project import Base


class OpsTests(Base):
    def test_postgres_operations(self):
        assert classify_op(pg_operations.CreateExtension("hstore")) == INERT
        assert classify_op(pg_operations.HStoreExtension()) == INERT
        assert classify_op(pg_operations.CreateCollation("c", "und")) == INERT
        assert classify_op(pg_operations.CollationOperation("c", "und")) == OPAQUE
