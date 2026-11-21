import json
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Client, LedgerEntry, LedgerTransaction, TransactionType
from app.schemas import TransactionCreate


def to_cents(amount: Decimal) -> int:
    return int(amount * 100)


def from_cents(amount_cents: int) -> Decimal:
    return Decimal(amount_cents).scaleb(-2)


def get_client(session: Session, tenant_id: str, client_id: int) -> Client:
    client = session.scalar(
        select(Client).where(Client.id == client_id, Client.tenant_id == tenant_id)
    )
    if client is None:
        raise LookupError("client not found")
    return client


def create_transaction(
    session: Session,
    tenant_id: str,
    payload: TransactionCreate,
) -> LedgerTransaction:
    amount_cents = to_cents(payload.amount)
    if payload.type == TransactionType.TRANSFER:
        source = get_client(session, tenant_id, payload.source_client_id)
        destination = get_client(session, tenant_id, payload.destination_client_id)
        if source.id == destination.id:
            raise ValueError("transfer clients must be different")
        if get_balance_cents(session, tenant_id, source.id) < amount_cents:
            raise ValueError("insufficient funds")
        client_id = None
    else:
        client = get_client(session, tenant_id, payload.client_id)
        if payload.type in (TransactionType.WITHDRAWAL, TransactionType.FEE):
            if get_balance_cents(session, tenant_id, client.id) < amount_cents:
                raise ValueError("insufficient funds")
        client_id = client.id

    transaction = LedgerTransaction(
        tenant_id=tenant_id,
        type=payload.type.value,
        amount_cents=amount_cents,
        idempotency_key=payload.idempotency_key,
        client_id=client_id,
        source_client_id=payload.source_client_id,
        destination_client_id=payload.destination_client_id,
        note=payload.note,
    )
    session.add(transaction)
    session.flush()

    if payload.type == TransactionType.TRANSFER:
        postings = ((payload.source_client_id, -amount_cents), (payload.destination_client_id, amount_cents))
    else:
        signed_amount = amount_cents if payload.type == TransactionType.DEPOSIT else -amount_cents
        postings = ((client_id, signed_amount),)
    for posted_client_id, signed_cents in postings:
        session.add(
            LedgerEntry(
                tenant_id=tenant_id,
                transaction_id=transaction.id,
                client_id=posted_client_id,
                amount_cents=signed_cents,
            )
        )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            transaction_id=transaction.id,
            action="transaction.created",
            details=json.dumps(
                {
                    "type": transaction.type,
                    "amount_cents": amount_cents,
                    "client_id": transaction.client_id,
                    "source_client_id": transaction.source_client_id,
                    "destination_client_id": transaction.destination_client_id,
                },
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return transaction


def get_balance_cents(session: Session, tenant_id: str, client_id: int) -> int:
    return session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.tenant_id == tenant_id,
            LedgerEntry.client_id == client_id,
        )
    )


def reverse_transaction(
    session: Session,
    tenant_id: str,
    transaction_id: int,
    reason: str,
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
        if get_balance_cents(session, tenant_id, entry.client_id) + -entry.amount_cents < 0:
            raise ValueError("reversal would create a negative client balance")
        session.add(
            LedgerEntry(
                tenant_id=tenant_id,
                transaction_id=reversal.id,
                client_id=entry.client_id,
                amount_cents=-entry.amount_cents,
            )
        )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            transaction_id=reversal.id,
            action="transaction.reversed",
            details=json.dumps(
                {"original_transaction_id": original.id, "reason": reason},
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return reversal
