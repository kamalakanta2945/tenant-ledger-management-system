from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import TransactionType

MAX_AMOUNT = Decimal("92233720368547758.07")


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    CAD = "CAD"
    AUD = "AUD"
    CHF = "CHF"
    NZD = "NZD"
    SGD = "SGD"


class ClientCreate(BaseModel):
    client_ref: str = Field(min_length=1, max_length=128)


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_ref: str


class TransactionCreate(BaseModel):
    type: TransactionType
    amount: Decimal
    idempotency_key: str = Field(min_length=1, max_length=128)
    currency: Currency = Currency.USD
    transaction_date: date | None = None
    client_ref: str | None = Field(default=None, min_length=1, max_length=128)
    source_client_ref: str | None = Field(default=None, min_length=1, max_length=128)
    destination_client_ref: str | None = Field(default=None, min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive_and_cent_exact(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("amount must be a positive finite decimal")
        if value > MAX_AMOUNT:
            raise ValueError("amount exceeds the supported range")
        if value != value.quantize(Decimal("0.01")):
            raise ValueError("amount must have no more than two decimal places")
        return value

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_cannot_be_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(timezone.utc).date():
            raise ValueError("transaction_date cannot be in the future")
        return value

    @model_validator(mode="after")
    def validate_transaction_parties(self) -> Self:
        if self.type in (TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.FEE):
            if (
                self.client_ref is None
                or self.source_client_ref is not None
                or self.destination_client_ref is not None
            ):
                raise ValueError("deposit, withdrawal, and fee require only client_ref")
        elif self.type == TransactionType.TRANSFER:
            if (
                self.client_ref is not None
                or self.source_client_ref is None
                or self.destination_client_ref is None
            ):
                raise ValueError("transfer requires source_client_ref and destination_client_ref only")
            if self.source_client_ref == self.destination_client_ref:
                raise ValueError("transfer clients must be different")
        else:
            raise ValueError("reversal transactions are created through the reversal endpoint")
        return self


class TransactionRead(BaseModel):
    id: int
    type: TransactionType
    amount: Decimal
    currency: Currency
    transaction_date: date
    client_ref: str | None
    source_client_ref: str | None
    destination_client_ref: str | None
    reversal_of_id: int | None
    idempotency_key: str
    note: str | None
    created_at: str


class ReversalCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class BulkTransactionCreate(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class BalanceRead(BaseModel):
    client_ref: str
    currency: Currency
    balance: Decimal
    as_of: date | None = None


class BulkRowResult(BaseModel):
    index: int
    transaction: TransactionRead | None = None
    error: str | None = None


class MonthlyReport(BaseModel):
    year: int
    month: int
    currency: Currency
    inflows: Decimal
    outflows: Decimal
    deposits: Decimal
    withdrawals: Decimal
    fees: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    reversals: Decimal
    net_change: Decimal
