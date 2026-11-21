from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, build_engine, build_session_factory, get_db
from app.models import AuditEvent, Client, LedgerEntry, LedgerTransaction, TransactionType
from app.schemas import (
    BalanceRead,
    BulkRowResult,
    BulkTransactionCreate,
    ClientCreate,
    ClientRead,
    MonthlyReport,
    ReversalCreate,
    TransactionCreate,
    TransactionRead,
)
from app.services import create_transaction, from_cents, get_balance_cents, get_client, reverse_transaction


def build_application(database_url: str | None = None) -> FastAPI:
    engine = build_engine(database_url)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        Base.metadata.create_all(engine)
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
        yield
        engine.dispose()

    application = FastAPI(title="Tenant Ledger API", version="1.0.0", lifespan=lifespan)
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)

    def require_tenant(x_tenant_id: Annotated[str, Header(min_length=1, max_length=128)]) -> str:
        return x_tenant_id

    Tenant = Annotated[str, Depends(require_tenant)]
    Db = Annotated[Session, Depends(get_db)]

    def transaction_read(transaction: LedgerTransaction) -> TransactionRead:
        return TransactionRead(
            id=transaction.id,
            type=transaction.type,
            amount=from_cents(transaction.amount_cents),
            client_id=transaction.client_id,
            source_client_id=transaction.source_client_id,
            destination_client_id=transaction.destination_client_id,
            reversal_of_id=transaction.reversal_of_id,
            idempotency_key=transaction.idempotency_key,
            note=transaction.note,
            created_at=transaction.created_at.isoformat(),
        )

    @application.post("/clients", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
    def create_client(payload: ClientCreate, tenant_id: Tenant, session: Db) -> Client:
        client = Client(tenant_id=tenant_id, external_id=payload.external_id, name=payload.name)
        session.add(client)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="client external_id already exists") from error
        session.refresh(client)
        return client

    @application.get("/clients", response_model=list[ClientRead])
    def list_clients(tenant_id: Tenant, session: Db) -> list[Client]:
        return session.scalars(
            select(Client).where(Client.tenant_id == tenant_id).order_by(Client.id)
        ).all()

    @application.post("/transactions", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
    def post_transaction(payload: TransactionCreate, tenant_id: Tenant, session: Db) -> TransactionRead:
        try:
            transaction = create_transaction(session, tenant_id, payload)
            session.commit()
        except LookupError as error:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(error)) from error
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="duplicate idempotency_key") from error
        return transaction_read(transaction)

    @application.post("/transactions/bulk", response_model=list[BulkRowResult])
    def bulk_transactions(
        payload: BulkTransactionCreate, tenant_id: Tenant, session: Db
    ) -> list[BulkRowResult]:
        results: list[BulkRowResult] = []
        for index, raw_item in enumerate(payload.items):
            try:
                item = TransactionCreate.model_validate(raw_item)
                with session.begin_nested():
                    transaction = create_transaction(session, tenant_id, item)
                    session.flush()
                results.append(BulkRowResult(index=index, transaction=transaction_read(transaction)))
            except (LookupError, ValueError, IntegrityError) as error:
                results.append(BulkRowResult(index=index, error=str(error)))
        session.commit()
        return results

    @application.get("/clients/{client_id}/balance", response_model=BalanceRead)
    def client_balance(client_id: int, tenant_id: Tenant, session: Db) -> BalanceRead:
        try:
            client = get_client(session, tenant_id, client_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return BalanceRead(client_id=client.id, balance=from_cents(get_balance_cents(session, tenant_id, client.id)))

    @application.post(
        "/transactions/{transaction_id}/reverse",
        response_model=TransactionRead,
        status_code=status.HTTP_201_CREATED,
    )
    def reverse(
        transaction_id: int, payload: ReversalCreate, tenant_id: Tenant, session: Db
    ) -> TransactionRead:
        try:
            transaction = reverse_transaction(session, tenant_id, transaction_id, payload.reason)
            session.commit()
        except LookupError as error:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="transaction already reversed") from error
        return transaction_read(transaction)

    @application.get("/reports/monthly", response_model=MonthlyReport)
    def monthly_report(
        year: int, month: int, tenant_id: Tenant, session: Db
    ) -> MonthlyReport:
        if month < 1 or month > 12 or year < 1 or year > 9998:
            raise HTTPException(status_code=422, detail="year and month must be valid")
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
        transaction_rows = session.execute(
            select(LedgerTransaction.type, LedgerTransaction.amount_cents).where(
                LedgerTransaction.tenant_id == tenant_id,
                LedgerTransaction.created_at >= start,
                LedgerTransaction.created_at < end,
            )
        ).all()
        entry_rows = session.execute(
            select(LedgerTransaction.type, LedgerEntry.amount_cents).join(
                LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id
            ).where(
                LedgerTransaction.tenant_id == tenant_id,
                LedgerEntry.tenant_id == tenant_id,
                LedgerTransaction.created_at >= start,
                LedgerTransaction.created_at < end,
            )
        ).all()
        totals = {name: 0 for name in ("deposits", "withdrawals", "fees", "transfers_in", "transfers_out", "reversals", "net_change")}
        for transaction_type, cents in transaction_rows:
            if transaction_type == TransactionType.DEPOSIT:
                totals["deposits"] += cents
            elif transaction_type == TransactionType.WITHDRAWAL:
                totals["withdrawals"] += cents
            elif transaction_type == TransactionType.FEE:
                totals["fees"] += cents
            elif transaction_type == TransactionType.REVERSAL:
                totals["reversals"] += cents
        for transaction_type, cents in entry_rows:
            totals["net_change"] += cents
            if transaction_type == TransactionType.TRANSFER:
                totals["transfers_in" if cents > 0 else "transfers_out"] += abs(cents)
        return MonthlyReport(
            year=year,
            month=month,
            **{name: from_cents(value) for name, value in totals.items()},
        )

    @application.get("/transactions/{transaction_id}/audit")
    def transaction_audit(
        transaction_id: int, tenant_id: Tenant, session: Db
    ) -> list[dict[str, object]]:
        transaction_exists = session.scalar(
            select(LedgerTransaction.id).where(
                LedgerTransaction.id == transaction_id,
                LedgerTransaction.tenant_id == tenant_id,
            )
        )
        if transaction_exists is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.transaction_id == transaction_id,
                AuditEvent.tenant_id == tenant_id,
            ).order_by(AuditEvent.id)
        ).all()
        return [
            {
                "id": item.id,
                "transaction_id": item.transaction_id,
                "action": item.action,
                "details": item.details,
                "created_at": item.created_at.isoformat(),
            }
            for item in events
        ]

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = build_application()
