from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


CurrencyLiteral = Literal["USD", "EUR", "RUB", "CNY"]


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TelegramAuthRequest(BaseModel):
    init_data: str = Field(..., min_length=1)


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: str
    first_name: str
    last_name: str

    model_config = {"from_attributes": True}


class TransactionCreate(BaseModel):
    amount: float = Field(..., gt=0)
    kind: Literal["income", "expense"]
    currency: CurrencyLiteral = "USD"
    category: str = Field(..., min_length=1, max_length=64)
    txn_date: date
    note: str = Field(default="", max_length=255)

    @field_validator("category")
    @classmethod
    def strip_category(cls, value: str) -> str:
        return value.strip()

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip()


class TransactionOut(BaseModel):
    id: int
    amount: float
    kind: str
    currency: str
    category: str
    txn_date: date
    note: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MonthlySummary(BaseModel):
    year: int
    month: int
    income: float
    expense: float
    balance: float


class CategorySummaryItem(BaseModel):
    category: str
    total: float


class BudgetUpsert(BaseModel):
    year: int = Field(..., ge=1900, le=2100)
    month: int = Field(..., ge=1, le=12)
    currency: CurrencyLiteral = "USD"
    category: str = Field(..., min_length=1, max_length=64)
    limit_amount: float = Field(..., gt=0)

    @field_validator("category")
    @classmethod
    def strip_category(cls, value: str) -> str:
        return value.strip()


class BudgetOut(BaseModel):
    id: int
    year: int
    month: int
    currency: str
    category: str
    limit_amount: float
    created_at: datetime

    model_config = {"from_attributes": True}


class BudgetStatusItem(BaseModel):
    category: str
    budget: float
    spent: float
    remaining: float
    over_limit: bool
