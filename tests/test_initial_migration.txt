"""Validate the frozen initial migration without an external database."""

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db import Base, models  # noqa: F401


def migration_sql(direction):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0001_initial.py"
    spec = importlib.util.spec_from_file_location("initial_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(module, direction)()
    return module, output.getvalue()


def test_initial_migration_is_a_single_frozen_root():
    module, sql = migration_sql("upgrade")
    assert module.revision == "0001_initial"
    assert module.down_revision is None
    for name in [*Base.metadata.tables, "chunk_search_index"]:
        assert f"CREATE TABLE {name} (" in sql
    assert "embedding halfvec(2560)" in sql
    assert "embedding halfvec_cosine_ops" in sql
    assert "GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED" in sql
    assert "WHERE is_current AND lifecycle_status = 'approved'" in sql
    assert "gin_trgm_ops" in sql
    assert "UPDATE " not in sql
    assert "INSERT INTO " not in sql


def test_downgrade_drops_all_application_tables_but_keeps_shared_extensions():
    _, sql = migration_sql("downgrade")
    for name in [*Base.metadata.tables, "chunk_search_index"]:
        assert f"DROP TABLE {name};" in sql
    assert sql.index("DROP TABLE chunk_search_index") < sql.index("DROP TABLE chunks;")
    assert sql.index("DROP TABLE chunks;") < sql.index("DROP TABLE document_sections;")
    assert "DROP EXTENSION" not in sql
