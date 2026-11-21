from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import TransactionType

MAX_AMOUNT = Decimal("92233720368547758.07")


class ClientCreate(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


class ClientRead(ClientCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class TransactionCreate(BaseModel):
    type: TransactionType
    amount: Decimal
    idempotency_key: str = Field(min_length=1, max_length=128)
    client_id: int | None = None
    source_client_id: int | None = None
    destination_client_id: int | None = None
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

    @model_validator(mode="after")
    def validate_transaction_parties(self) -> Self:
        if self.type in (TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.FEE):
            if self.client_id is None or self.source_client_id is not None or self.destination_client_id is not None:
                raise ValueError("deposit, withdrawal, and fee require only client_id")
        elif self.type == TransactionType.TRANSFER:
            if self.client_id is not None or self.source_client_id is None or self.destination_client_id is None:
                raise ValueError("transfer requires source_client_id and destination_client_id only")
            if self.source_client_id == self.destination_client_id:
                raise ValueError("transfer clients must be different")
        else:
            raise ValueError("reversal transactions are created through the reversal endpoint")
        return self


class TransactionRead(BaseModel):
    id: int
    type: TransactionType
    amount: Decimal
    client_id: int | None
    source_client_id: int | None
    destination_client_id: int | None
    reversal_of_id: int | None
    idempotency_key: str
    note: str | None
    created_at: str


class ReversalCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class BulkTransactionCreate(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class BalanceRead(BaseModel):
    client_id: int
    balance: Decimal


class BulkRowResult(BaseModel):
    index: int
    transaction: TransactionRead | None = None
    error: str | None = None


class MonthlyReport(BaseModel):
    year: int
    month: int
    deposits: Decimal
    withdrawals: Decimal
    fees: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    reversals: Decimal
    net_change: Decimal
