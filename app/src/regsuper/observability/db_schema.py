"""Render the database schema as Markdown or JSON.

The live renderers query the running PostgreSQL catalogue (`pg_class`, `information_schema`),
so they show what is really in the database, including generated columns (`tsv`), indexes
created by SQL migrations, and check constraints the ORM does not declare.

Two sets of renderers:

* `render_markdown_live()` / `render_json_live()`: database introspection (preferred).
* `render_markdown()` / `render_json()`: ORM metadata, used as a fallback when the database is
  unreachable.
"""

from __future__ import annotations

from sqlalchemy import text

from regsuper.storage.database import get_engine
from regsuper.storage.models import Base


def _format_type(col) -> str:
    """Render a column's SQL type compactly, e.g. VECTOR(1536), TEXT[], JSONB."""
    try:
        t = str(col.type)
    except Exception:
        t = repr(col.type)
    return t


async def reflect_live_schema() -> dict:
    """Introspect every table in the public schema.

    Returns the same JSON shape as `render_json()`, so one renderer serves both sources.
    """
    engine = get_engine()
    out_tables: list[dict] = []

    async with engine.connect() as conn:
        # Tables in roughly declaration order (creation order by pg_class.oid), excluding the
        # internal tables PostGIS installs.
        _SKIP = {
            "spatial_ref_sys",
            "geography_columns",
            "geometry_columns",
            "raster_columns",
            "raster_overviews",
        }
        tbl_rows = (
            await conn.execute(
                text(
                    "SELECT c.relname AS table_name "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "ORDER BY c.oid"
                )
            )
        ).all()
        table_names = [r.table_name for r in tbl_rows if r.table_name not in _SKIP]

        for tname in table_names:
            col_rows = (
                await conn.execute(
                    text(
                        "SELECT "
                        "  column_name, "
                        "  CASE "
                        "    WHEN data_type = 'ARRAY' THEN udt_name "
                        "    WHEN data_type = 'USER-DEFINED' THEN udt_name "
                        "    ELSE data_type "
                        "  END AS dtype, "
                        "  is_nullable, "
                        "  column_default, "
                        "  is_generated, "
                        "  generation_expression "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t "
                        "ORDER BY ordinal_position"
                    ),
                    {"t": tname},
                )
            ).all()

            # Primary key columns.
            pk_rows = (
                await conn.execute(
                    text(
                        "SELECT a.attname AS column_name "
                        "FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                        "                   AND a.attnum = ANY(i.indkey) "
                        "JOIN pg_class c ON c.oid = i.indrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname='public' AND c.relname = :t AND i.indisprimary"
                    ),
                    {"t": tname},
                )
            ).all()
            pk_cols = {r.column_name for r in pk_rows}

            # Foreign keys, per column.
            fk_rows = (
                await conn.execute(
                    text(
                        "SELECT "
                        "  kcu.column_name, "
                        "  ccu.table_name AS ref_table, "
                        "  ccu.column_name AS ref_column, "
                        "  rc.delete_rule "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "  ON tc.constraint_name = kcu.constraint_name "
                        " AND tc.table_schema = kcu.table_schema "
                        "JOIN information_schema.constraint_column_usage ccu "
                        "  ON ccu.constraint_name = tc.constraint_name "
                        " AND ccu.table_schema = tc.table_schema "
                        "JOIN information_schema.referential_constraints rc "
                        "  ON rc.constraint_name = tc.constraint_name "
                        "WHERE tc.constraint_type='FOREIGN KEY' "
                        "  AND tc.table_schema='public' "
                        "  AND tc.table_name = :t"
                    ),
                    {"t": tname},
                )
            ).all()
            fk_by_col: dict[str, list[dict]] = {}
            for r in fk_rows:
                fk_by_col.setdefault(r.column_name, []).append(
                    {
                        "references": f"{r.ref_table}.{r.ref_column}",
                        "on_delete": r.delete_rule,
                    }
                )

            # Indexes — name, columns, unique.
            idx_rows = (
                await conn.execute(
                    text(
                        "SELECT i.relname AS index_name, "
                        "       ARRAY(SELECT pg_get_indexdef(i.oid, k + 1, true) "
                        "             FROM generate_subscripts(ix.indkey, 1) AS k) AS cols, "
                        "       ix.indisunique AS is_unique, "
                        "       am.amname AS method "
                        "FROM pg_index ix "
                        "JOIN pg_class i ON i.oid = ix.indexrelid "
                        "JOIN pg_class t ON t.oid = ix.indrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "JOIN pg_am am ON am.oid = i.relam "
                        "WHERE n.nspname='public' AND t.relname = :t "
                        "  AND NOT ix.indisprimary "
                        "ORDER BY i.relname"
                    ),
                    {"t": tname},
                )
            ).all()

            # Check constraints (excluding NOT NULLs which show up via columns).
            chk_rows = (
                await conn.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(c.oid) AS def "
                        "FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "WHERE n.nspname='public' AND t.relname = :t AND c.contype = 'c' "
                        "ORDER BY conname"
                    ),
                    {"t": tname},
                )
            ).all()

            cols_out = []
            for c in col_rows:
                cols_out.append(
                    {
                        "name": c.column_name,
                        "type": c.dtype,
                        "nullable": c.is_nullable == "YES",
                        "primary_key": c.column_name in pk_cols,
                        "unique": False,  # captured via UNIQUE index list instead
                        "foreign_keys": fk_by_col.get(c.column_name, []),
                        "default": c.column_default,
                        "generated": c.is_generated != "NEVER",
                        "generation_expression": c.generation_expression,
                    }
                )

            out_tables.append(
                {
                    "name": tname,
                    "columns": cols_out,
                    "indexes": [
                        {
                            "name": r.index_name,
                            "columns": list(r.cols or []),
                            "unique": r.is_unique,
                            "method": r.method,
                        }
                        for r in idx_rows
                    ],
                    "check_constraints": [
                        {
                            "name": r.conname,
                            "definition": r.def_ if hasattr(r, "def_") else r._mapping["def"],
                        }
                        for r in chk_rows
                    ],
                }
            )

    return {"tables": out_tables, "n_tables": len(out_tables), "source": "live_db"}


async def render_markdown_live() -> str:
    """Reflect the live DB and emit the same Markdown shape as render_markdown()."""
    data = await reflect_live_schema()
    return _json_to_markdown(data)


def _json_to_markdown(data: dict) -> str:
    md: list[str] = ["# Database schema", ""]
    src = data.get("source", "model_metadata")
    if src == "live_db":
        md.append(
            "_Reflected from the live PostgreSQL `information_schema`. "
            "Includes generated columns, indexes, and constraints "
            "as the database actually holds them._"
        )
    else:
        md.append("_Rendered from SQLAlchemy declarative metadata (DB not reachable)._")
    md.append("")
    md.append(f"**{data['n_tables']} tables**.")
    md.append("")

    md.append("## Tables")
    md.append("")
    for t in data["tables"]:
        md.append(f"- [`{t['name']}`](#{t['name']})")
    md.append("")

    for t in data["tables"]:
        md.append(f'## `{t["name"]}` <a id="{t["name"]}"></a>')
        md.append("")
        md.append("| Column | Type | Nullable | Key | Default |")
        md.append("|---|---|---|---|---|")
        for col in t["columns"]:
            key_parts: list[str] = []
            if col.get("primary_key"):
                key_parts.append("PK")
            for fk in col.get("foreign_keys") or []:
                key_parts.append(f"FK→{fk['references']}")
            key = ", ".join(key_parts) or ""
            nullable = "" if not col.get("nullable") else "✓"
            default = ""
            if col.get("generated"):
                default = f"GENERATED `{(col.get('generation_expression') or '')[:60]}`"
            elif col.get("default"):
                default = f"`{str(col['default'])[:60]}`"
            md.append(f"| `{col['name']}` | `{col['type']}` | {nullable} | {key} | {default} |")
        md.append("")

        if t.get("indexes"):
            md.append("**Indexes**")
            md.append("")
            for ix in t["indexes"]:
                cols = ", ".join(ix["columns"])
                tag = " (unique)" if ix.get("unique") else ""
                method = (
                    f" [{ix.get('method')}]" if ix.get("method") and ix["method"] != "btree" else ""
                )
                md.append(f"- `{ix['name']}`{tag}{method} on ({cols})")
            md.append("")

        if t.get("check_constraints"):
            md.append("**Constraints**")
            md.append("")
            for c in t["check_constraints"]:
                md.append(f"- `{c['name']}`: `{c['definition']}`")
            md.append("")

    return "\n".join(md)


def render_markdown() -> str:
    """Produce a single Markdown document describing every mapped table.

    Tables are emitted in declaration order so related tables stay near each
    other (source_document → provision → provision_sentence → ...).
    """
    md: list[str] = ["# Database schema", ""]
    md.append(
        "_Auto-rendered from SQLAlchemy declarative metadata. Reflects the "
        "live schema **as the Python models declare it** — if a migration "
        "diverges, this view will still show the model side._"
    )
    md.append("")
    tables = list(Base.metadata.tables.values())
    md.append(f"**{len(tables)} tables**.")
    md.append("")

    # Table of contents
    md.append("## Tables")
    md.append("")
    for t in tables:
        md.append(f"- [`{t.name}`](#{t.name})")
    md.append("")

    for t in tables:
        md.append(f'## `{t.name}` <a id="{t.name}"></a>')
        md.append("")
        if t.comment:
            md.append(f"_{t.comment}_")
            md.append("")

        md.append("| Column | Type | Nullable | Key | Default |")
        md.append("|---|---|---|---|---|")
        for col in t.columns:
            key_parts: list[str] = []
            if col.primary_key:
                key_parts.append("PK")
            fk_rendered: list[str] = []
            for fk in col.foreign_keys:
                ref = fk.target_fullname
                fk_rendered.append(f"FK→{ref}")
            key_parts.extend(fk_rendered)
            if col.unique:
                key_parts.append("UQ")
            key = ", ".join(key_parts) or ""
            nullable = "" if col.nullable else "**NOT NULL**"
            default = ""
            if col.server_default is not None:
                default = "`" + str(col.server_default.arg)[:60] + "`"
            elif col.default is not None:
                if hasattr(col.default, "arg"):
                    default = f"`{col.default.arg!r}`"[:60]
                else:
                    default = "(default)"
            md.append(f"| `{col.name}` | `{_format_type(col)}` | {nullable} | {key} | {default} |")
        md.append("")

        # Indexes
        if t.indexes:
            md.append("**Indexes**")
            md.append("")
            for ix in sorted(t.indexes, key=lambda i: i.name or ""):
                cols = ", ".join(c.name for c in ix.columns)
                tag = " (unique)" if ix.unique else ""
                md.append(f"- `{ix.name}`{tag} on ({cols})")
            md.append("")

        # Constraints
        check_constraints = [c for c in t.constraints if c.__class__.__name__ == "CheckConstraint"]
        if check_constraints:
            md.append("**Constraints**")
            md.append("")
            for c in check_constraints:
                md.append(f"- `{c.name or '(unnamed)'}`: `{c.sqltext}`")
            md.append("")

    return "\n".join(md)


def render_json() -> dict:
    """Return the ORM schema as structured JSON for the interactive schema explorer.

    Lives next to the Markdown renderer so both views share one field set.
    """
    out_tables = []
    for t in Base.metadata.tables.values():
        cols = []
        for col in t.columns:
            fks = [{"references": fk.target_fullname} for fk in col.foreign_keys]
            cols.append(
                {
                    "name": col.name,
                    "type": _format_type(col),
                    "nullable": col.nullable,
                    "primary_key": col.primary_key,
                    "unique": col.unique,
                    "foreign_keys": fks,
                }
            )
        indexes = [
            {"name": ix.name, "columns": [c.name for c in ix.columns], "unique": ix.unique}
            for ix in t.indexes
        ]
        out_tables.append(
            {
                "name": t.name,
                "columns": cols,
                "indexes": indexes,
            }
        )
    return {"tables": out_tables, "n_tables": len(out_tables)}
