from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlite3
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.database import build_session_factory
from app.main import build_application
from app.models import AuditEvent, Client, LedgerEntry, LedgerTransaction


@pytest.fixture
def client(tmp_path):
    database_path = tmp_path / "ledger.sqlite"
    application = build_application(f"sqlite:///{database_path}")
    with TestClient(application) as test_client:
        yield test_client


def headers(tenant: str, actor: str = "test-actor") -> dict[str, str]:
    return {"X-Tenant-ID": tenant, "X-Actor-ID": actor}


def create_client(client: TestClient, tenant: str, client_ref: str = "client-a") -> str:
    response = client.post(
        "/clients",
        headers=headers(tenant),
        json={"client_ref": client_ref},
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"client_ref"}
    return response.json()["client_ref"]


def post_transaction(client: TestClient, tenant: str, **payload):
    return client.post("/transactions", headers=headers(tenant), json=payload)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def test_tenant_isolation_and_opaque_client_references(client):
    client_a = create_client(client, "tenant-a", "external-ref-01")
    client_b = create_client(client, "tenant-b", "external-ref-01")
    deposited = post_transaction(
        client,
        "tenant-a",
        type="deposit",
        amount="125.50",
        idempotency_key="deposit-1",
        client_ref=client_a,
    )
    assert deposited.status_code == 201, deposited.text
    assert "client_id" not in deposited.json()
    assert "name" not in deposited.json()
    assert [row["client_ref"] for row in client.get("/clients", headers=headers("tenant-b")).json()] == [
        client_b
    ]
    assert client.get(
        f"/clients/{client_b}/balance", headers=headers("tenant-b")
    ).json()["balance"] == "0.00"
    assert client.get("/transactions", headers=headers("tenant-b")).json() == []
    cross_tenant = post_transaction(
        client,
        "tenant-b",
        type="withdrawal",
        amount="1.00",
        idempotency_key="cross-tenant",
        client_ref="not-owned-by-tenant-b",
    )
    assert cross_tenant.status_code == 404
    transaction_id = deposited.json()["id"]
    assert client.get(
        f"/transactions/{transaction_id}/audit", headers=headers("tenant-b")
    ).status_code == 404
    assert client.post(
        f"/transactions/{transaction_id}/reverse",
        headers=headers("tenant-b"),
        json={"reason": "cross-tenant attempt"},
    ).status_code == 404


def test_financial_validation_currency_dates_duplicates_and_balance(client):
    customer = create_client(client, "acme")
    for amount in ("-1.00", "0", "1.001"):
        invalid = post_transaction(
            client,
            "acme",
            type="deposit",
            amount=amount,
            idempotency_key=f"invalid-{amount}",
            client_ref=customer,
        )
        assert invalid.status_code == 422

    invalid_currency = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="1.00",
        currency="usd",
        idempotency_key="bad-currency",
        client_ref=customer,
    )
    assert invalid_currency.status_code == 422
    unsupported_currency = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="1.00",
        currency="JPY",
        idempotency_key="unsupported-currency",
        client_ref=customer,
    )
    assert unsupported_currency.status_code == 422
    future = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="1.00",
        transaction_date=(utc_today() + timedelta(days=1)).isoformat(),
        idempotency_key="future",
        client_ref=customer,
    )
    assert future.status_code == 422

    deposited = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="10.25",
        currency="EUR",
        idempotency_key="same",
        client_ref=customer,
    )
    assert deposited.status_code == 201, deposited.text
    assert deposited.json()["currency"] == "EUR"
    assert deposited.json()["transaction_date"] == utc_today().isoformat()
    duplicate = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="10.25",
        currency="EUR",
        idempotency_key="same",
        client_ref=customer,
    )
    assert duplicate.status_code == 409
    overdraft = post_transaction(
        client,
        "acme",
        type="withdrawal",
        amount="10.26",
        currency="EUR",
        idempotency_key="overdraft",
        client_ref=customer,
    )
    assert overdraft.status_code == 422
    assert client.get(
        f"/clients/{customer}/balance?currency=EUR", headers=headers("acme")
    ).json()["balance"] == "10.25"
    assert client.get(
        f"/clients/{customer}/balance?currency=USD", headers=headers("acme")
    ).json()["balance"] == "0.00"


def test_withdrawal_fee_transfer_reversal_and_audit(client):
    source = create_client(client, "acme", "source-ref")
    target = create_client(client, "acme", "target-ref")
    deposit = post_transaction(
        client, "acme", type="deposit", amount="50.00", idempotency_key="fund", client_ref=source
    )
    assert deposit.status_code == 201
    transfer = post_transaction(
        client,
        "acme",
        type="transfer",
        amount="12.34",
        idempotency_key="move",
        source_client_ref=source,
        destination_client_ref=target,
    )
    assert transfer.status_code == 201, transfer.text
    withdrawal = post_transaction(
        client,
        "acme",
        type="withdrawal",
        amount="2.00",
        idempotency_key="withdrawal",
        client_ref=target,
    )
    assert withdrawal.status_code == 201, withdrawal.text
    fee = post_transaction(
        client, "acme", type="fee", amount="1.00", idempotency_key="fee", client_ref=target
    )
    assert fee.status_code == 201, fee.text
    top_up = post_transaction(
        client, "acme", type="deposit", amount="3.00", idempotency_key="top-up", client_ref=target
    )
    assert top_up.status_code == 201, top_up.text
    assert client.get(f"/clients/{source}/balance", headers=headers("acme")).json()["balance"] == "37.66"
    assert client.get(f"/clients/{target}/balance", headers=headers("acme")).json()["balance"] == "12.34"

    reversed_transfer = client.post(
        f"/transactions/{transfer.json()['id']}/reverse",
        headers=headers("acme", "reviewer-7"),
        json={"reason": "entered in error"},
    )
    assert reversed_transfer.status_code == 201, reversed_transfer.text
    assert client.get(f"/clients/{source}/balance", headers=headers("acme")).json()["balance"] == "50.00"
    assert client.get(f"/clients/{target}/balance", headers=headers("acme")).json()["balance"] == "0.00"
    assert client.post(
        f"/transactions/{transfer.json()['id']}/reverse",
        headers=headers("acme"),
        json={"reason": "again"},
    ).status_code == 409
    history = client.get(
        f"/transactions/{transfer.json()['id']}/audit", headers=headers("acme")
    ).json()
    assert [event["action"] for event in history] == [
        "transaction.created",
        "transaction.reversed",
    ]
    assert {event["tenant_id"] for event in history} == {"acme"}
    assert [event["actor_id"] for event in history] == ["test-actor", "reviewer-7"]

    now = datetime.now(timezone.utc)
    report = client.get(
        f"/reports/monthly?year={now.year}&month={now.month}&currency=USD",
        headers=headers("acme"),
    )
    assert report.status_code == 200, report.text
    assert report.json()["fees"] == "1.00"
    assert report.json()["transfers_in"] == "24.68"
    assert report.json()["transfers_out"] == "24.68"
    assert report.json()["inflows"] == "53.00"
    assert report.json()["outflows"] == "3.00"
    assert report.json()["reversals"] == "12.34"
    assert report.json()["net_change"] == "50.00"


def test_bulk_import_isolates_validation_errors_and_duplicates(client):
    customer = create_client(client, "acme")
    response = client.post(
        "/transactions/bulk",
        headers=headers("acme"),
        json={
            "items": [
                {"type": "deposit", "amount": "5.00", "idempotency_key": "bulk-1", "client_ref": customer},
                {"type": "withdrawal", "amount": "6.00", "idempotency_key": "bulk-2", "client_ref": customer},
                {"type": "deposit", "amount": "2.00", "idempotency_key": "bulk-3", "client_ref": customer},
                {"type": "deposit", "amount": "not-money", "idempotency_key": "bulk-4", "client_ref": customer},
                {"type": "deposit", "amount": "1.00", "idempotency_key": "bulk-1", "client_ref": customer},
                {"type": "deposit", "amount": "1.00", "idempotency_key": "bulk-5", "client_ref": customer},
            ]
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows[0]["transaction"] is not None
    assert "insufficient funds" in rows[1]["error"]
    assert rows[2]["transaction"] is not None
    assert rows[3]["transaction"] is None and rows[3]["error"]
    assert rows[4]["transaction"] is None and rows[4]["error"]
    assert rows[4]["error"] == "duplicate idempotency_key"
    assert rows[5]["transaction"] is not None
    assert client.get(f"/clients/{customer}/balance", headers=headers("acme")).json()["balance"] == "8.00"
    retried = client.post(
        "/transactions/bulk",
        headers=headers("acme"),
        json={
            "items": [
                {"type": "deposit", "amount": "5.00", "idempotency_key": "bulk-1", "client_ref": customer}
            ]
        },
    )
    assert retried.json()[0]["transaction"] is None


def test_historical_balance_includes_reversals_on_their_effective_date(client):
    customer = create_client(client, "acme")
    today = utc_today()
    first_day = today - timedelta(days=20)
    middle_day = today - timedelta(days=10)
    deposit = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="20.00",
        transaction_date=first_day.isoformat(),
        idempotency_key="history-deposit",
        client_ref=customer,
    )
    withdrawal = post_transaction(
        client,
        "acme",
        type="withdrawal",
        amount="5.00",
        transaction_date=middle_day.isoformat(),
        idempotency_key="history-withdrawal",
        client_ref=customer,
    )
    assert deposit.status_code == withdrawal.status_code == 201
    assert client.get(
        f"/clients/{customer}/balance/historical?as_of={first_day.isoformat()}",
        headers=headers("acme"),
    ).json()["balance"] == "20.00"
    assert client.get(
        f"/clients/{customer}/balance/historical?as_of={middle_day.isoformat()}",
        headers=headers("acme"),
    ).json()["balance"] == "15.00"
    backdated_overdraft = post_transaction(
        client,
        "acme",
        type="withdrawal",
        amount="1.00",
        transaction_date=(first_day - timedelta(days=1)).isoformat(),
        idempotency_key="backdated-overdraft",
        client_ref=customer,
    )
    assert backdated_overdraft.status_code == 422
    reversal = client.post(
        f"/transactions/{withdrawal.json()['id']}/reverse",
        headers=headers("acme", "reviewer"),
        json={"reason": "withdrawal correction"},
    )
    assert reversal.status_code == 201
    assert client.get(
        f"/clients/{customer}/balance/historical?as_of={middle_day.isoformat()}",
        headers=headers("acme"),
    ).json()["balance"] == "15.00"
    assert client.get(
        f"/clients/{customer}/balance/historical?as_of={today.isoformat()}",
        headers=headers("acme"),
    ).json()["balance"] == "20.00"
    future = today + timedelta(days=1)
    assert client.get(
        f"/clients/{customer}/balance/historical?as_of={future.isoformat()}",
        headers=headers("acme"),
    ).status_code == 422
    quarter_end = client.get(
        f"/clients/{customer}/balance/previous-quarter-end", headers=headers("acme")
    )
    assert quarter_end.status_code == 200
    expected_quarter_end = (
        date(today.year, ((today.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    )
    assert quarter_end.json()["as_of"] == expected_quarter_end.isoformat()


def test_concurrent_writes_prevent_overdraft_duplicates_and_repeat_reversals(client):
    customer = create_client(client, "acme")
    assert post_transaction(
        client, "acme", type="deposit", amount="100.00", idempotency_key="seed", client_ref=customer
    ).status_code == 201

    def withdraw(key: str):
        return post_transaction(
            client,
            "acme",
            type="withdrawal",
            amount="80.00",
            idempotency_key=key,
            client_ref=customer,
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(withdraw, ("withdraw-1", "withdraw-2")))
    assert sorted(results) == [201, 422]
    assert client.get(f"/clients/{customer}/balance", headers=headers("acme")).json()["balance"] == "20.00"

    def deposit_same_key():
        return post_transaction(
            client,
            "acme",
            type="deposit",
            amount="1.00",
            idempotency_key="duplicate-concurrent",
            client_ref=customer,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate_futures = [pool.submit(deposit_same_key) for _ in range(2)]
        duplicate_results = [future.result() for future in duplicate_futures]
    assert sorted(response.status_code for response in duplicate_results) == [201, 409]
    deposit_id = next(response.json()["id"] for response in duplicate_results if response.status_code == 201)

    def reverse_deposit():
        return client.post(
            f"/transactions/{deposit_id}/reverse",
            headers=headers("acme"),
            json={"reason": "concurrency check"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reversal_futures = [pool.submit(reverse_deposit) for _ in range(2)]
        reversal_results = [future.result() for future in reversal_futures]
    assert sorted(response.status_code for response in reversal_results) == [201, 409]
    assert client.get(f"/clients/{customer}/balance", headers=headers("acme")).json()["balance"] == "20.00"


def test_immutable_audit_history_and_documented_routes(client):
    customer = create_client(client, "acme")
    transaction = post_transaction(
        client,
        "acme",
        type="deposit",
        amount="1.00",
        idempotency_key="audit",
        client_ref=customer,
    )
    assert transaction.status_code == 201
    assert client.get("/transactions", headers=headers("acme")).json()[0]["id"] == transaction.json()["id"]
    assert client.get("/docs").status_code == 200
    assert "/clients/{client_ref}/balance/historical" in client.get("/openapi.json").json()["paths"]

    session_factory = build_session_factory(client.app.state.engine)
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(AuditEvent).values(details="tampered"))
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(delete(AuditEvent))
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(update(LedgerTransaction).values(note="changed"))
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(delete(LedgerEntry))
            session.commit()
        session.rollback()
        assert session.scalar(select(AuditEvent.details)) != "tampered"


def test_database_constraints_reject_cross_tenant_ledger_reference(client):
    create_client(client, "tenant-a", "tenant-a-ref")
    create_client(client, "tenant-b", "tenant-b-ref")
    session_factory = build_session_factory(client.app.state.engine)
    with session_factory() as session:
        foreign_client_id = session.scalar(
            select(Client.id).where(Client.tenant_id == "tenant-b")
        )
        session.add(
            LedgerTransaction(
                tenant_id="tenant-a",
                type="deposit",
                amount_cents=200,
                currency="USD",
                transaction_date=datetime.now(timezone.utc).date(),
                idempotency_key="invalid-cross-tenant",
                client_id=foreign_client_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    assert client.get("/transactions?limit=501", headers=headers("tenant-a")).status_code == 422


def test_legacy_sqlite_database_migration_removes_client_name_and_preserves_ledger(tmp_path):
    database_path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY,
            tenant_id VARCHAR(128) NOT NULL,
            external_id VARCHAR(128) NOT NULL,
            name VARCHAR(200) NOT NULL,
            created_at DATETIME NOT NULL,
            UNIQUE (tenant_id, external_id),
            UNIQUE (tenant_id, id)
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            tenant_id VARCHAR(128) NOT NULL,
            type VARCHAR(20) NOT NULL,
            amount_cents INTEGER NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            client_id INTEGER,
            source_client_id INTEGER,
            destination_client_id INTEGER,
            reversal_of_id INTEGER,
            note TEXT,
            created_at DATETIME NOT NULL,
            UNIQUE (tenant_id, idempotency_key),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, reversal_of_id),
            FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
            FOREIGN KEY (tenant_id, source_client_id) REFERENCES clients (tenant_id, id),
            FOREIGN KEY (tenant_id, destination_client_id) REFERENCES clients (tenant_id, id),
            FOREIGN KEY (tenant_id, reversal_of_id) REFERENCES transactions (tenant_id, id)
        );
        CREATE TABLE ledger_entries (
            id INTEGER PRIMARY KEY,
            tenant_id VARCHAR(128) NOT NULL,
            transaction_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            UNIQUE (transaction_id, client_id),
            FOREIGN KEY (tenant_id, transaction_id) REFERENCES transactions (tenant_id, id),
            FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id)
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY,
            tenant_id VARCHAR(128) NOT NULL,
            transaction_id INTEGER NOT NULL,
            action VARCHAR(40) NOT NULL,
            details TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (tenant_id, transaction_id) REFERENCES transactions (tenant_id, id)
        );
        INSERT INTO clients VALUES (1, 'legacy-tenant', 'legacy-ref', 'Private Name', '2026-03-01 10:00:00+00:00');
        INSERT INTO transactions VALUES (
            1, 'legacy-tenant', 'deposit', 700, 'legacy-key', 1, NULL, NULL, NULL, NULL,
            '2026-03-01 10:00:00+00:00'
        );
        INSERT INTO ledger_entries VALUES (1, 'legacy-tenant', 1, 1, 700);
        INSERT INTO audit_events VALUES (
            1, 'legacy-tenant', 1, 'transaction.created', '{}', '2026-03-01 10:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    application = build_application(f"sqlite:///{database_path}")
    with TestClient(application) as legacy_client:
        assert legacy_client.get("/clients", headers=headers("legacy-tenant")).json() == [
            {"client_ref": "legacy-ref"}
        ]
        migrated_transaction = legacy_client.get(
            "/transactions", headers=headers("legacy-tenant")
        ).json()[0]
        assert migrated_transaction["transaction_date"] == "2026-03-01"
        assert migrated_transaction["currency"] == "USD"
        assert legacy_client.get(
            "/clients/legacy-ref/balance", headers=headers("legacy-tenant")
        ).json()["balance"] == "7.00"
        assert legacy_client.get(
            f"/transactions/{migrated_transaction['id']}/audit",
            headers=headers("legacy-tenant"),
        ).json()[0]["actor_id"] == "unknown"
