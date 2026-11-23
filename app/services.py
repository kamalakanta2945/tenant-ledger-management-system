import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Client, LedgerEntry, LedgerTransaction, TransactionType
from app.schemas import TransactionCreate


def to_cents(amount: Decimal) -> int:
    return int(amount * 100)


def from_cents(amount_cents: int) -> Decimal:
    return Decimal(amount_cents).scaleb(-2)


def begin_write_transaction(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def get_client_by_ref(session: Session, tenant_id: str, client_ref: str) -> Client:
    client = session.scalar(
        select(Client).where(Client.client_ref == client_ref, Client.tenant_id == tenant_id)
    )
    if client is None:
        raise LookupError("client not found")
    return client


def get_client_by_id(session: Session, tenant_id: str, client_id: int | None) -> Client | None:
    if client_id is None:
        return None
    return session.scalar(
        select(Client).where(Client.id == client_id, Client.tenant_id == tenant_id)
    )


def create_transaction(
    session: Session,
    tenant_id: str,
    payload: TransactionCreate,
    actor_id: str,
) -> LedgerTransaction:
    amount_cents = to_cents(payload.amount)
    currency = payload.currency.value
    transaction_date = payload.transaction_date or datetime.now(timezone.utc).date()
    if transaction_date > datetime.now(timezone.utc).date():
        raise ValueError("transaction_date cannot be in the future")

    if payload.type == TransactionType.TRANSFER:
        source = get_client_by_ref(session, tenant_id, payload.source_client_ref)
        destination = get_client_by_ref(session, tenant_id, payload.destination_client_ref)
        if source.id == destination.id:
            raise ValueError("transfer clients must be different")
        if (
            get_balance_cents(
                session, tenant_id, source.id, currency, transaction_date
            )
            < amount_cents
        ):
            raise ValueError("insufficient funds")
        client_id = None
        source_client_id = source.id
        destination_client_id = destination.id
    else:
        client = get_client_by_ref(session, tenant_id, payload.client_ref)
        if payload.type in (TransactionType.WITHDRAWAL, TransactionType.FEE):
            if (
                get_balance_cents(
                    session, tenant_id, client.id, currency, transaction_date
                )
                < amount_cents
            ):
                raise ValueError("insufficient funds")
        client_id = client.id
        source_client_id = None
        destination_client_id = None

    transaction = LedgerTransaction(
        tenant_id=tenant_id,
        type=payload.type.value,
        amount_cents=amount_cents,
        currency=currency,
        transaction_date=transaction_date,
        idempotency_key=payload.idempotency_key,
        client_id=client_id,
        source_client_id=source_client_id,
        destination_client_id=destination_client_id,
        note=payload.note,
    )
    session.add(transaction)
    session.flush()

    if payload.type == TransactionType.TRANSFER:
        postings = ((source_client_id, -amount_cents), (destination_client_id, amount_cents))
    else:
        signed_amount = amount_cents if payload.type == TransactionType.DEPOSIT else -amount_cents
        postings = ((client_id, signed_amount),)
    for posted_client_id, signed_cents in postings:
        session.add(
            LedgerEntry(
                tenant_id=tenant_id,
                transaction_id=transaction.id,
                client_id=posted_client_id,
                currency=currency,
                amount_cents=signed_cents,
            )
        )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            transaction_id=transaction.id,
            action="transaction.created",
            actor_id=actor_id,
            details=json.dumps(
                {
                    "type": transaction.type,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "client_ref": payload.client_ref,
                    "source_client_ref": payload.source_client_ref,
                    "destination_client_ref": payload.destination_client_ref,
                },
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return transaction


def get_balance_cents(
    session: Session,
    tenant_id: str,
    client_id: int,
    currency: str,
    as_of: date | None = None,
) -> int:
    query = select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
        LedgerEntry.tenant_id == tenant_id,
        LedgerEntry.client_id == client_id,
        LedgerEntry.currency == currency,
    )
    if as_of is not None:
        query = query.join(
            LedgerTransaction,
            (LedgerTransaction.id == LedgerEntry.transaction_id)
            & (LedgerTransaction.tenant_id == LedgerEntry.tenant_id),
        ).where(LedgerTransaction.transaction_date <= as_of)
    return session.scalar(query)


def reverse_transaction(
    session: Session,
    tenant_id: str,
    transaction_id: int,
    reason: str,
    actor_id: str,
) -> LedgerTransaction:
    original = session.scalar(
        select(LedgerTransaction).where(
            LedgerTransaction.id == transaction_id,
            LedgerTransaction.tenant_id == tenant_id,
        )
    )
    if original is None:
        raise LookupError("transaction not found")
    if original.type == TransactionType.REVERSAL:
        raise ValueError("a reversal cannot itself be reversed")
    if session.scalar(
        select(LedgerTransaction.id).where(
            LedgerTransaction.tenant_id == tenant_id,
            LedgerTransaction.reversal_of_id == original.id,
        )
    ) is not None:
        raise ValueError("transaction has already been reversed")

    reversal = LedgerTransaction(
        tenant_id=tenant_id,
        type=TransactionType.REVERSAL.value,
        amount_cents=original.amount_cents,
        currency=original.currency,
        transaction_date=datetime.now(timezone.utc).date(),
        idempotency_key=f"reversal:{original.id}",
        client_id=original.client_id,
        source_client_id=original.source_client_id,
        destination_client_id=original.destination_client_id,
        reversal_of_id=original.id,
        note=reason,
    )
    session.add(reversal)
    session.flush()
    original_entries = session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.transaction_id == original.id,
            LedgerEntry.tenant_id == tenant_id,
        )
    ).all()
    for entry in original_entries:
        session.add(
            LedgerEntry(
                tenant_id=tenant_id,
                transaction_id=reversal.id,
                client_id=entry.client_id,
                currency=entry.currency,
                amount_cents=-entry.amount_cents,
            )
        )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            transaction_id=reversal.id,
            action="transaction.reversed",
            actor_id=actor_id,
            details=json.dumps(
                {"original_transaction_id": original.id, "reason": reason},
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return reversal
