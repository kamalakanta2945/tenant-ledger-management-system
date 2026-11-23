import os
from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./tenant_ledger.db")
    options = (
        {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url.startswith("sqlite")
        else {}
    )
    engine = create_engine(url, **options)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            del _record
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def migrate_sqlite_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        version = tuple(
            int(part)
            for part in connection.exec_driver_sql("SELECT sqlite_version()")
            .scalar_one()
            .split(".")
        )
        if version < (3, 35, 0):
            raise RuntimeError("SQLite 3.35 or later is required to remove stored client names")

        inspector = inspect(connection)
        if "clients" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("clients")}
            if "external_id" in columns and "client_ref" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE clients RENAME COLUMN external_id TO client_ref"
                )
                columns.remove("external_id")
                columns.add("client_ref")
            if "name" in columns:
                connection.exec_driver_sql("ALTER TABLE clients DROP COLUMN name")

        if "transactions" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("transactions")}
            if "currency" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE transactions ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'"
                )
            if "transaction_date" not in columns:
                connection.exec_driver_sql("ALTER TABLE transactions ADD COLUMN transaction_date DATE")
                connection.exec_driver_sql(
                    "UPDATE transactions SET transaction_date = substr(created_at, 1, 10) "
                    "WHERE transaction_date IS NULL"
                )

        if "ledger_entries" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("ledger_entries")}
            if "currency" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE ledger_entries ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'"
                )
                connection.exec_driver_sql(
                    "UPDATE ledger_entries SET currency = (SELECT currency FROM transactions "
                    "WHERE transactions.id = ledger_entries.transaction_id)"
                )

        if "audit_events" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("audit_events")}
            if "actor_id" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE audit_events ADD COLUMN actor_id VARCHAR(128) NOT NULL DEFAULT 'unknown'"
                )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_tenant_currency_date "
            "ON transactions (tenant_id, currency, transaction_date)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_tenant_created_id "
            "ON transactions (tenant_id, created_at, id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_entries_tenant_client_currency "
            "ON ledger_entries (tenant_id, client_id, currency)"
        )


def install_sqlite_guards(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS transactions_no_update
            BEFORE UPDATE ON transactions
            BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS transactions_no_delete
            BEFORE DELETE ON transactions
            BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
            BEFORE UPDATE ON ledger_entries
            BEGIN SELECT RAISE(ABORT, 'ledger entries are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
            BEFORE DELETE ON ledger_entries
            BEGIN SELECT RAISE(ABORT, 'ledger entries are immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS transactions_validate_currency
            BEFORE INSERT ON transactions
            WHEN NEW.currency NOT IN ('USD', 'EUR', 'GBP', 'CAD', 'AUD', 'CHF', 'NZD', 'SGD')
            BEGIN SELECT RAISE(ABORT, 'unsupported currency'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS ledger_entries_prevent_overdraft
            BEFORE INSERT ON ledger_entries
            WHEN NEW.amount_cents + COALESCE(
                (SELECT SUM(existing_entry.amount_cents)
                 FROM ledger_entries AS existing_entry
                 JOIN transactions AS existing_transaction
                   ON existing_transaction.id = existing_entry.transaction_id
                  AND existing_transaction.tenant_id = existing_entry.tenant_id
                 JOIN transactions AS new_transaction
                   ON new_transaction.id = NEW.transaction_id
                  AND new_transaction.tenant_id = NEW.tenant_id
                 WHERE existing_entry.tenant_id = NEW.tenant_id
                   AND existing_entry.client_id = NEW.client_id
                   AND existing_entry.currency = NEW.currency
                   AND existing_transaction.transaction_date <= new_transaction.transaction_date), 0
            ) < 0
            BEGIN SELECT RAISE(ABORT, 'insufficient funds'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS ledger_entries_validate_transaction_currency
            BEFORE INSERT ON ledger_entries
            WHEN NOT EXISTS (
                SELECT 1 FROM transactions
                WHERE id = NEW.transaction_id
                  AND tenant_id = NEW.tenant_id
                  AND currency = NEW.currency
            )
            BEGIN SELECT RAISE(ABORT, 'ledger entry transaction or currency mismatch'); END
            """
        )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session
