from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.database import build_session_factory
from app.main import build_application
from app.models import AuditEvent


@pytest.fixture
def client(tmp_path):
    database_path = tmp_path / "ledger.sqlite"
    application = build_application(f"sqlite:///{database_path}")
    with TestClient(application) as test_client:
        yield test_client


def headers(tenant: str) -> dict[str, str]:
    return {"X-Tenant-ID": tenant}


def create_client(client: TestClient, tenant: str, external_id: str = "c1") -> int:
    response = client.post(
        "/clients",
        headers=headers(tenant),
        json={"external_id": external_id, "name": f"Client {external_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def post_transaction(client: TestClient, tenant: str, **payload):
    return client.post("/transactions", headers=headers(tenant), json=payload)


def test_tenant_isolation_for_clients_balances_and_transactions(client):
    tenant_a_client = create_client(client, "tenant-a")
    tenant_b_client = create_client(client, "tenant-b")
    assert tenant_a_client != tenant_b_client

    deposited = post_transaction(
        client,
        "tenant-a",
        type="deposit",
        amount="125.50",
        idempotency_key="deposit-1",
        client_id=tenant_a_client,
    )
    assert deposited.status_code == 201, deposited.text
    assert [row["id"] for row in client.get("/clients", headers=headers("tenant-b")).json()] == [
        tenant_b_client
    ]
    assert client.get(
        f"/clients/{tenant_b_client}/balance", headers=headers("tenant-b")
    ).json()["balance"] == "0.00"
    cross_tenant = post_transaction(
        client,
        "tenant-b",
        type="withdrawal",
        amount="1.00",
        idempotency_key="cross-tenant",
        client_id=tenant_a_client,
    )
    assert cross_tenant.status_code == 404
    assert client.get(
        f"/transactions/{deposited.json()['id']}/audit", headers=headers("tenant-b")
    ).status_code == 404


def test_money_validation_duplicate_prevention_and_insufficient_funds(client):
    customer = create_client(client, "acme")
    invalid = post_transaction(
        client, "acme", type="deposit", amount="1.001", idempotency_key="bad", client_id=customer
    )
    assert invalid.status_code == 422
    too_large = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="92233720368547758.08",
        idempotency_key="too-large",
        client_id=customer,
    )
    assert too_large.status_code == 422
    deposited = post_transaction(
        client, "acme", type="deposit", amount="10.25", idempotency_key="same", client_id=customer
    )
    assert deposited.status_code == 201, deposited.text
    duplicate = post_transaction(
        client, "acme", type="deposit", amount="10.25", idempotency_key="same", client_id=customer
    )
    assert duplicate.status_code == 409
    overdraft = post_transaction(
        client, "acme", type="withdrawal", amount="10.26", idempotency_key="overdraft", client_id=customer
    )
    assert overdraft.status_code == 422
    balance = client.get(f"/clients/{customer}/balance", headers=headers("acme")).json()
    assert Decimal(balance["balance"]) == Decimal("10.25")


def test_transfer_fee_reversal_and_immutable_audit(client):
    source = create_client(client, "acme", "source")
    target = create_client(client, "acme", "target")
    deposit = post_transaction(
        client, "acme", type="deposit", amount="50.00", idempotency_key="fund", client_id=source
    )
    assert deposit.status_code == 201
    transfer = post_transaction(
        client,
        "acme",
        type="transfer",
        amount="12.34",
        idempotency_key="move",
        source_client_id=source,
        destination_client_id=target,
    )
    assert transfer.status_code == 201, transfer.text
    fee = post_transaction(
        client, "acme", type="fee", amount="1.00", idempotency_key="fee", client_id=target
    )
    assert fee.status_code == 201, fee.text
    top_up = post_transaction(
        client, "acme", type="deposit", amount="1.00", idempotency_key="top-up", client_id=target
    )
    assert top_up.status_code == 201, top_up.text
    assert client.get(f"/clients/{source}/balance", headers=headers("acme")).json()["balance"] == "37.66"
    assert client.get(f"/clients/{target}/balance", headers=headers("acme")).json()["balance"] == "12.34"

    reversed_transfer = client.post(
        f"/transactions/{transfer.json()['id']}/reverse",
        headers=headers("acme"),
        json={"reason": "entered in error"},
    )
    assert reversed_transfer.status_code == 201, reversed_transfer.text
    assert client.get(f"/clients/{source}/balance", headers=headers("acme")).json()["balance"] == "50.00"
    assert client.get(f"/clients/{target}/balance", headers=headers("acme")).json()["balance"] == "0.00"
    duplicate_reversal = client.post(
        f"/transactions/{transfer.json()['id']}/reverse",
        headers=headers("acme"),
        json={"reason": "again"},
    )
    assert duplicate_reversal.status_code == 409
    audit = client.get(
        f"/transactions/{transfer.json()['id']}/audit", headers=headers("acme")
    ).json()
    assert len(audit) == 1
    now = datetime.now(timezone.utc)
    report = client.get(
        f"/reports/monthly?year={now.year}&month={now.month}", headers=headers("acme")
    )
    assert report.json()["reversals"] == "12.34"
    assert report.json()["transfers_in"] == "12.34"
    assert report.json()["transfers_out"] == "12.34"
    assert report.json()["fees"] == "1.00"


def test_bulk_import_isolates_bad_rows(client):
    customer = create_client(client, "acme")
    response = client.post(
        "/transactions/bulk",
        headers=headers("acme"),
        json={
            "items": [
                {"type": "deposit", "amount": "5.00", "idempotency_key": "bulk-1", "client_id": customer},
                {"type": "withdrawal", "amount": "6.00", "idempotency_key": "bulk-2", "client_id": customer},
                {"type": "deposit", "amount": "2.00", "idempotency_key": "bulk-3", "client_id": customer},
                {"type": "deposit", "amount": "not-money", "idempotency_key": "bulk-4", "client_id": customer},
                {"type": "deposit", "amount": "1.00", "idempotency_key": "bulk-5", "client_id": customer},
            ]
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows[0]["transaction"] is not None
    assert "insufficient funds" in rows[1]["error"]
    assert rows[2]["transaction"] is not None
    assert rows[3]["transaction"] is None
    assert rows[3]["error"]
    assert rows[4]["transaction"] is not None
    assert client.get(f"/clients/{customer}/balance", headers=headers("acme")).json()["balance"] == "8.00"


def test_monthly_report_and_reversal_cannot_overdraw(client):
    customer = create_client(client, "acme")
    deposit = post_transaction(
        client, "acme", type="deposit", amount="5.00", idempotency_key="deposit", client_id=customer
    )
    withdrawal = post_transaction(
        client, "acme", type="withdrawal", amount="2.00", idempotency_key="withdraw", client_id=customer
    )
    reversed_deposit = client.post(
        f"/transactions/{deposit.json()['id']}/reverse",
        headers=headers("acme"),
        json={"reason": "correction"},
    )
    assert reversed_deposit.status_code == 409
    now = datetime.now(timezone.utc)
    report = client.get(
        f"/reports/monthly?year={now.year}&month={now.month}", headers=headers("acme")
    )
    assert report.status_code == 200, report.text
    assert report.json()["deposits"] == "5.00"
    assert report.json()["withdrawals"] == "2.00"
    assert report.json()["net_change"] == "3.00"
    assert withdrawal.status_code == 201


def test_audit_records_reject_mutation(client):
    customer = create_client(client, "acme")
    transaction = post_transaction(
        client, "acme", type="deposit", amount="1.00", idempotency_key="audit", client_id=customer
    )
    assert transaction.status_code == 201
    engine = client.app.state.engine
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(AuditEvent).values(details="tampered"))
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(delete(AuditEvent))
            session.commit()
        session.rollback()
        assert session.scalar(select(AuditEvent.details)) != "tampered"
