"""Idempotent upgrade of pre-authentication installations; never deletes data."""

from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, select, text

from app.db.models import AuthBootstrap, Repository, User


def migrate_auth_schema(engine) -> None:
    additions = {
        "users": {"username": "VARCHAR(80)", "password_hash": "TEXT", "is_active": "BOOLEAN NOT NULL DEFAULT TRUE"},
        "repositories": {"owner_user_id": "{uuid} REFERENCES users(id)"},
        "analysis_results": {"owner_user_id": "{uuid} REFERENCES users(id)"},
        "eval_runs": {"owner_user_id": "{uuid} REFERENCES users(id)"},
    }
    uuid_type = User.__table__.c.id.type.compile(dialect=engine.dialect)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type.format(uuid=uuid_type)}'))
        if "users" in tables:
            connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)'))
        for table in ("repositories", "analysis_results", "eval_runs"):
            if table in tables:
                connection.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table}_owner_user_id ON "{table}" (owner_user_id)'))

    # Two users may connect the same upstream repository without sharing tokens,
    # conversations, vectors or filesystem checkouts.
    inspector = inspect(engine)
    if "repositories" in tables:
        preparer = engine.dialect.identifier_preparer
        with engine.begin() as connection:
            for index in inspector.get_indexes("repositories"):
                if index.get("unique") and not index.get("duplicates_constraint") and index.get("column_names") == ["full_name"]:
                    connection.execute(text(f'DROP INDEX {preparer.quote(index["name"])}'))
            constraints = inspector.get_unique_constraints("repositories")
            if engine.dialect.name == "sqlite":
                # SQLite's SQL-text reflection can miss inline UNIQUE in legacy
                # one-line DDL. PRAGMA is authoritative for table constraints.
                for index in connection.exec_driver_sql('PRAGMA index_list("repositories")').mappings():
                    if index["origin"] != "u":
                        continue
                    columns = [row["name"] for row in connection.exec_driver_sql(
                        f'PRAGMA index_info({preparer.quote(index["name"])})'
                    ).mappings()]
                    if columns == ["full_name"] and not any(item["column_names"] == columns for item in constraints):
                        constraints.append({"name": None, "column_names": columns})
            for constraint in constraints:
                if constraint.get("column_names") != ["full_name"]:
                    continue
                if engine.dialect.name == "sqlite":
                    from alembic.migration import MigrationContext
                    from alembic.operations import Operations

                    operations = Operations(MigrationContext.configure(connection))
                    legacy_table = Table("repositories", MetaData(), autoload_with=connection)
                    constraint_name = constraint.get("name") or "uq_repositories_full_name"
                    reflected = next((item for item in legacy_table.constraints
                                      if isinstance(item, UniqueConstraint) and list(item.columns.keys()) == ["full_name"]), None)
                    if reflected is None:
                        legacy_table.append_constraint(UniqueConstraint("full_name", name=constraint_name))
                    else:
                        reflected.name = constraint_name
                    with operations.batch_alter_table(
                        "repositories", copy_from=legacy_table
                    ) as batch:
                        batch.drop_constraint(constraint_name, type_="unique")
                else:
                    connection.execute(text(f'ALTER TABLE repositories DROP CONSTRAINT {preparer.quote(constraint["name"])}'))
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_repositories_full_name ON repositories (full_name)"))
        unique_names = {item.get("name") for item in inspect(engine).get_unique_constraints("repositories")}
        if "uq_repository_owner_full_name" not in unique_names:
            with engine.begin() as connection:
                connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_owner_full_name ON repositories (owner_user_id, full_name)"))
    with engine.begin() as connection:
        if connection.execute(select(AuthBootstrap.id).where(AuthBootstrap.id == 1)).scalar_one_or_none() is None:
            existing_owner = connection.execute(
                select(User.id).where(User.password_hash.is_not(None)).order_by(User.created_at).limit(1)
            ).scalar_one_or_none()
            connection.execute(AuthBootstrap.__table__.insert().values(id=1, claimed_by=existing_owner))
