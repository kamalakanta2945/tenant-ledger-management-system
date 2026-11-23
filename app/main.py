from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, TypeAlias

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.database import (
    Base,
    build_engine,
    build_session_factory,
    get_db,
    install_sqlite_guards,
    migrate_sqlite_schema,
)
from app.models import AuditEvent, Client, LedgerEntry, LedgerTransaction, TransactionType
from app.schemas import (
    BalanceRead,
    BulkRowResult,
    BulkTransactionCreate,
    ClientCreate,
    ClientRead,
    Currency,
    MonthlyReport,
    ReversalCreate,
    TransactionCreate,
    TransactionRead,
)
from app.services import (
    begin_write_transaction,
    create_transaction,
    from_cents,
    get_balance_cents,
    get_client_by_id,
    get_client_by_ref,
    reverse_transaction,
)


def build_application(database_url: str | None = None) -> FastAPI:
    engine = build_engine(database_url)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        del _application
        Base.metadata.create_all(engine)
        migrate_sqlite_schema(engine)
        install_sqlite_guards(engine)
        yield
        engine.dispose()

    application = FastAPI(title="Tenant Ledger API", version="1.0.0", lifespan=lifespan)
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)

    def require_tenant(x_tenant_id: Annotated[str, Header(min_length=1, max_length=128)]) -> str:
        return x_tenant_id

    Tenant: TypeAlias = Annotated[str, Depends(require_tenant)]
    Db: TypeAlias = Annotated[Session, Depends(get_db)]

    def transaction_read(session: Session, transaction: LedgerTransaction) -> TransactionRead:
        client = get_client_by_id(session, transaction.tenant_id, transaction.client_id)
        source = get_client_by_id(session, transaction.tenant_id, transaction.source_client_id)
        destination = get_client_by_id(
            session, transaction.tenant_id, transaction.destination_client_id
        )
        return TransactionRead(
            id=transaction.id,
            type=transaction.type,
            amount=from_cents(transaction.amount_cents),
            currency=transaction.currency,
            transaction_date=transaction.transaction_date,
            client_ref=client.client_ref if client else None,
            source_client_ref=source.client_ref if source else None,
            destination_client_ref=destination.client_ref if destination else None,
            reversal_of_id=transaction.reversal_of_id,
            idempotency_key=transaction.idempotency_key,
            note=transaction.note,
            created_at=(
                transaction.created_at.replace(tzinfo=timezone.utc)
                if transaction.created_at.tzinfo is None
                else transaction.created_at
            ).isoformat(),
        )

    @application.post("/clients", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
    def create_client(payload: ClientCreate, tenant_id: Tenant, session: Db) -> Client:
        client = Client(tenant_id=tenant_id, client_ref=payload.client_ref)
        session.add(client)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="client_ref already exists") from error
        session.refresh(client)
        return client

    @application.get("/clients", response_model=list[ClientRead])
    def list_clients(tenant_id: Tenant, session: Db) -> list[Client]:
        return session.scalars(
            select(Client).where(Client.tenant_id == tenant_id).order_by(Client.id)
        ).all()

    @application.post("/transactions", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
    def post_transaction(
        payload: TransactionCreate,
        tenant_id: Tenant,
        session: Db,
        actor_id: Annotated[
            str | None, Header(alias="X-Actor-ID", min_length=1, max_length=128)
        ] = None,
    ) -> TransactionRead:
        try:
            begin_write_transaction(session)
            transaction = create_transaction(session, tenant_id, payload, actor_id or "unknown")
            session.commit()
        except LookupError as error:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(error)) from error
        except IntegrityError as error:
            session.rollback()
            detail = (
                "duplicate idempotency_key"
                if "transactions.tenant_id, transactions.idempotency_key" in str(error.orig)
                else "transaction violated a database constraint"
            )
            raise HTTPException(status_code=409, detail=detail) from error
        return transaction_read(session, transaction)

    @application.post("/transactions/bulk", response_model=list[BulkRowResult])
    def bulk_transactions(
        payload: BulkTransactionCreate,
        tenant_id: Tenant,
        session: Db,
        actor_id: Annotated[
            str | None, Header(alias="X-Actor-ID", min_length=1, max_length=128)
        ] = None,
    ) -> list[BulkRowResult]:
        results: list[BulkRowResult] = []
        begin_write_transaction(session)
        for index, raw_item in enumerate(payload.items):
            try:
                item = TransactionCreate.model_validate(raw_item)
                with session.begin_nested():
                    transaction = create_transaction(
                        session, tenant_id, item, actor_id or "unknown"
                    )
                    session.flush()
                results.append(
                    BulkRowResult(
                        index=index, transaction=transaction_read(session, transaction)
                    )
                )
            except (LookupError, ValueError, IntegrityError) as error:
                if isinstance(error, IntegrityError):
                    duplicate = session.scalar(
                        select(LedgerTransaction.id).where(
                            LedgerTransaction.tenant_id == tenant_id,
                            LedgerTransaction.idempotency_key
                            == raw_item.get("idempotency_key"),
                        )
                    )
                    message = (
                        "duplicate idempotency_key"
                        if duplicate is not None
                        else "database constraint rejected this row"
                    )
                else:
                    message = str(error)
                results.append(BulkRowResult(index=index, error=message))
        session.commit()
        return results

    def balance_response(
        client_ref: str,
        currency: Currency,
        as_of: date | None,
        tenant_id: str,
        session: Session,
    ) -> BalanceRead:
        try:
            client = get_client_by_ref(session, tenant_id, client_ref)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return BalanceRead(
            client_ref=client.client_ref,
            currency=currency,
            balance=from_cents(
                get_balance_cents(session, tenant_id, client.id, currency.value, as_of)
            ),
            as_of=as_of,
        )

    @application.get("/clients/{client_ref}/balance", response_model=BalanceRead)
    def client_balance(
        client_ref: str,
        tenant_id: Tenant,
        session: Db,
        currency: Currency = Currency.USD,
    ) -> BalanceRead:
        return balance_response(client_ref, currency, None, tenant_id, session)

    @application.get("/clients/{client_ref}/balance/historical", response_model=BalanceRead)
    def historical_client_balance(
        client_ref: str,
        as_of: date,
        tenant_id: Tenant,
        session: Db,
        currency: Currency = Currency.USD,
    ) -> BalanceRead:
        if as_of > datetime.now(timezone.utc).date():
            raise HTTPException(status_code=422, detail="as_of cannot be in the future")
        return balance_response(client_ref, currency, as_of, tenant_id, session)

    @application.get(
        "/clients/{client_ref}/balance/previous-quarter-end", response_model=BalanceRead
    )
    def previous_quarter_end_balance(
        client_ref: str,
        tenant_id: Tenant,
        session: Db,
        currency: Currency = Currency.USD,
    ) -> BalanceRead:
        today = datetime.now(timezone.utc).date()
        current_quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        quarter_start = date(today.year, current_quarter_start_month, 1)
        previous_quarter_end = quarter_start - timedelta(days=1)
        return balance_response(
            client_ref, currency, previous_quarter_end, tenant_id, session
        )

    @application.get("/transactions", response_model=list[TransactionRead])
    def list_transactions(
        tenant_id: Tenant,
        session: Db,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TransactionRead]:
        if limit < 1 or limit > 500 or offset < 0:
            raise HTTPException(status_code=422, detail="limit must be 1..500 and offset nonnegative")
        transactions = session.scalars(
            select(LedgerTransaction)
            .where(LedgerTransaction.tenant_id == tenant_id)
            .order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [transaction_read(session, transaction) for transaction in transactions]

    @application.post(
        "/transactions/{transaction_id}/reverse",
        response_model=TransactionRead,
        status_code=status.HTTP_201_CREATED,
    )
    def reverse(
        transaction_id: int,
        payload: ReversalCreate,
        tenant_id: Tenant,
        session: Db,
        actor_id: Annotated[
            str | None, Header(alias="X-Actor-ID", min_length=1, max_length=128)
        ] = None,
    ) -> TransactionRead:
        try:
            begin_write_transaction(session)
            transaction = reverse_transaction(
                session, tenant_id, transaction_id, payload.reason, actor_id or "unknown"
            )
            session.commit()
        except LookupError as error:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        except IntegrityError as error:
            session.rollback()
            detail = (
                "transaction has already been reversed"
                if "transactions.tenant_id, transactions.reversal_of_id" in str(error.orig)
                else "reversal violated a database constraint"
            )
            raise HTTPException(status_code=409, detail=detail) from error
        return transaction_read(session, transaction)

    @application.get("/reports/monthly", response_model=MonthlyReport)
    def monthly_report(
        year: int,
        month: int,
        tenant_id: Tenant,
        session: Db,
        currency: Currency = Currency.USD,
    ) -> MonthlyReport:
        if month < 1 or month > 12 or year < 1 or year > 9998:
            raise HTTPException(status_code=422, detail="year and month must be valid")
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        original_transaction = aliased(LedgerTransaction)
        query = (
            select(
                LedgerTransaction.type,
                LedgerEntry.amount_cents,
                original_transaction.type,
            )
            .join(
                LedgerEntry,
                (LedgerEntry.transaction_id == LedgerTransaction.id)
                & (LedgerEntry.tenant_id == LedgerTransaction.tenant_id),
            )
            .outerjoin(
                original_transaction,
                (LedgerTransaction.reversal_of_id == original_transaction.id)
                & (LedgerTransaction.tenant_id == original_transaction.tenant_id),
            )
            .where(
                LedgerTransaction.tenant_id == tenant_id,
                LedgerEntry.tenant_id == tenant_id,
                LedgerTransaction.currency == currency.value,
                LedgerTransaction.transaction_date >= start,
                LedgerTransaction.transaction_date < end,
            )
        )
        rows = session.execute(query).all()
        reversal_rows = session.execute(
            select(LedgerTransaction.amount_cents).where(
                LedgerTransaction.tenant_id == tenant_id,
                LedgerTransaction.currency == currency.value,
                LedgerTransaction.type == TransactionType.REVERSAL,
                LedgerTransaction.transaction_date >= start,
                LedgerTransaction.transaction_date < end,
            )
        ).all()
        totals = {
            name: 0
            for name in (
                "deposits",
                "withdrawals",
                "fees",
                "transfers_in",
                "transfers_out",
                "reversals",
                "net_change",
                "inflows",
                "outflows",
            )
        }
        for transaction_type, entry_cents, original_type in rows:
            root_type = original_type if transaction_type == TransactionType.REVERSAL else transaction_type
            totals["net_change"] += entry_cents
            if root_type == TransactionType.TRANSFER:
                if entry_cents > 0:
                    totals["transfers_in"] += entry_cents
                else:
                    totals["transfers_out"] += -entry_cents
            elif entry_cents > 0:
                totals["inflows"] += entry_cents
            else:
                totals["outflows"] += -entry_cents
            if root_type == TransactionType.DEPOSIT:
                totals["deposits"] += entry_cents
            elif root_type == TransactionType.WITHDRAWAL:
                totals["withdrawals"] -= entry_cents
            elif root_type == TransactionType.FEE:
                totals["fees"] -= entry_cents
        totals["reversals"] = sum(row[0] for row in reversal_rows)
        return MonthlyReport(
            year=year,
            month=month,
            currency=currency,
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
            select(AuditEvent)
            .join(
                LedgerTransaction,
                (LedgerTransaction.id == AuditEvent.transaction_id)
                & (LedgerTransaction.tenant_id == AuditEvent.tenant_id),
            )
            .where(
                AuditEvent.tenant_id == tenant_id,
            )
            .where(
                or_(
                    LedgerTransaction.id == transaction_id,
                    LedgerTransaction.reversal_of_id == transaction_id,
                )
            )
            .order_by(AuditEvent.id)
        ).all()
        return [
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "transaction_id": item.transaction_id,
                "action": item.action,
                "actor_id": item.actor_id,
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
