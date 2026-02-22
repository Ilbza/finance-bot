import base64
import csv
import hashlib
import hmac
import io
import json
import os
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import BudgetEntry, Transaction, User
from .schemas import (
    BudgetOut,
    BudgetStatusItem,
    BudgetUpsert,
    CategorySummaryItem,
    LoginResponse,
    MonthlySummary,
    TelegramAuthRequest,
    TransactionCreate,
    TransactionOut,
    UserOut,
)

Base.metadata.create_all(bind=engine)


app = FastAPI(title="Finance Tracker API", version="0.3.0")
security = HTTPBearer(auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


TOKEN_TTL_HOURS = int(os.getenv("APP_TOKEN_TTL_HOURS", "24"))
AUTH_SECRET = os.getenv("APP_SECRET", "change-me-in-env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_AUTH_MAX_AGE_SECONDS = int(os.getenv("TG_AUTH_MAX_AGE_SECONDS", "86400"))


def _ensure_schema_compatibility():
    with engine.begin() as connection:
        txn_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(transactions)")).fetchall()}
        if "currency" not in txn_columns:
            connection.execute(text("ALTER TABLE transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'"))
        if "user_id" not in txn_columns:
            connection.execute(text("ALTER TABLE transactions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"))


_ensure_schema_compatibility()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    payload = {"uid": user_id, "exp": int(exp.timestamp())}
    payload_raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_raw)

    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    signature_b64 = _b64url_encode(signature)

    return f"{payload_b64}.{signature_b64}"


def verify_access_token(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    payload_b64, signature_b64 = parts
    expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()

    try:
        provided_sig = _b64url_decode(signature_b64)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    if not hmac.compare_digest(provided_sig, expected_sig):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    exp = int(payload.get("exp", 0))
    if datetime.now(tz=timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")

    user_id = int(payload.get("uid", 0))
    if user_id <= 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    return user_id


def verify_telegram_init_data(init_data: str) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Server is not configured for Telegram auth")

    parsed_pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed_pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram hash")

    data_check_string = "\n".join(f"{key}={parsed_pairs[key]}" for key in sorted(parsed_pairs.keys()))
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")

    auth_date = int(parsed_pairs.get("auth_date", "0"))
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    if auth_date <= 0 or now_ts - auth_date > TG_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram auth data expired")

    user_raw = parsed_pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Telegram user payload is missing")

    try:
        user_data = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload") from exc

    if not user_data.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user id is missing")

    return user_data


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id = verify_access_token(credentials.credentials)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/auth/telegram", response_model=LoginResponse)
def auth_telegram(payload: TelegramAuthRequest, db: Session = Depends(get_db)):
    tg_user = verify_telegram_init_data(payload.init_data)

    telegram_id = int(tg_user["id"])
    username = (tg_user.get("username") or "").strip()
    first_name = (tg_user.get("first_name") or "").strip()
    last_name = (tg_user.get("last_name") or "").strip()

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
    else:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        db.add(user)

    db.commit()
    db.refresh(user)

    return LoginResponse(access_token=create_access_token(user.id))


@app.get("/auth/me", response_model=UserOut)
def auth_me(current_user: User = Depends(get_current_user)):
    return current_user


@app.post("/transactions", response_model=TransactionOut)
def create_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transaction = Transaction(
        user_id=current_user.id,
        amount=payload.amount,
        kind=payload.kind,
        currency=payload.currency,
        category=payload.category,
        txn_date=payload.txn_date,
        note=payload.note,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


@app.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    kind: Optional[Literal["income", "expense"]] = Query(default=None),
    currency: Optional[Literal["USD", "EUR", "RUB", "CNY"]] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Transaction).filter(Transaction.user_id == current_user.id)

    if start_date:
        query = query.filter(Transaction.txn_date >= start_date)
    if end_date:
        query = query.filter(Transaction.txn_date <= end_date)
    if category:
        query = query.filter(Transaction.category == category.strip())
    if kind:
        query = query.filter(Transaction.kind == kind)
    if currency:
        query = query.filter(Transaction.currency == currency)

    return query.order_by(Transaction.txn_date.desc(), Transaction.id.desc()).all()


@app.get("/transactions/export.csv")
def export_transactions_csv(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    kind: Optional[Literal["income", "expense"]] = Query(default=None),
    currency: Optional[Literal["USD", "EUR", "RUB", "CNY"]] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Transaction).filter(Transaction.user_id == current_user.id)

    if start_date:
        query = query.filter(Transaction.txn_date >= start_date)
    if end_date:
        query = query.filter(Transaction.txn_date <= end_date)
    if category:
        query = query.filter(Transaction.category == category.strip())
    if kind:
        query = query.filter(Transaction.kind == kind)
    if currency:
        query = query.filter(Transaction.currency == currency)

    transactions = query.order_by(Transaction.txn_date.desc(), Transaction.id.desc()).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "date", "type", "currency", "category", "amount", "note", "created_at"])

    for txn in transactions:
        writer.writerow(
            [
                txn.id,
                txn.txn_date.isoformat(),
                txn.kind,
                txn.currency,
                txn.category,
                txn.amount,
                txn.note,
                txn.created_at.isoformat(),
            ]
        )

    csv_content = buffer.getvalue()
    filename = f"transactions-{date.today().isoformat()}.csv"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/transactions/{transaction_id}")
def delete_transaction(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transaction = (
        db.query(Transaction)
        .filter(Transaction.id == transaction_id, Transaction.user_id == current_user.id)
        .first()
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.delete(transaction)
    db.commit()
    return {"ok": True}


@app.get("/summary/month", response_model=MonthlySummary)
def monthly_summary(
    year: int = Query(..., ge=1900, le=2100),
    month: int = Query(..., ge=1, le=12),
    currency: Literal["USD", "EUR", "RUB", "CNY"] = Query(default="USD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    income = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.kind == "income",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .scalar()
    )
    expense = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.kind == "expense",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .scalar()
    )

    return MonthlySummary(
        year=year,
        month=month,
        income=float(income or 0),
        expense=float(expense or 0),
        balance=float((income or 0) - (expense or 0)),
    )


@app.get("/summary/categories", response_model=list[CategorySummaryItem])
def category_summary(
    year: int = Query(..., ge=1900, le=2100),
    month: int = Query(..., ge=1, le=12),
    kind: Literal["income", "expense"] = Query(default="expense"),
    currency: Literal["USD", "EUR", "RUB", "CNY"] = Query(default="USD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    rows = (
        db.query(Transaction.category, func.sum(Transaction.amount).label("total"))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.kind == kind,
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
        .all()
    )

    return [CategorySummaryItem(category=row.category, total=float(row.total)) for row in rows]


@app.post("/budgets", response_model=BudgetOut)
def upsert_budget(
    payload: BudgetUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    budget = (
        db.query(BudgetEntry)
        .filter(
            BudgetEntry.user_id == current_user.id,
            BudgetEntry.year == payload.year,
            BudgetEntry.month == payload.month,
            BudgetEntry.currency == payload.currency,
            BudgetEntry.category == payload.category,
        )
        .first()
    )

    if budget:
        budget.limit_amount = payload.limit_amount
    else:
        budget = BudgetEntry(
            user_id=current_user.id,
            year=payload.year,
            month=payload.month,
            currency=payload.currency,
            category=payload.category,
            limit_amount=payload.limit_amount,
        )
        db.add(budget)

    db.commit()
    db.refresh(budget)
    return budget


@app.get("/budgets", response_model=list[BudgetOut])
def list_budgets(
    year: int = Query(..., ge=1900, le=2100),
    month: int = Query(..., ge=1, le=12),
    currency: Literal["USD", "EUR", "RUB", "CNY"] = Query(default="USD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(BudgetEntry)
        .filter(
            BudgetEntry.user_id == current_user.id,
            BudgetEntry.year == year,
            BudgetEntry.month == month,
            BudgetEntry.currency == currency,
        )
        .order_by(BudgetEntry.category.asc())
        .all()
    )


@app.delete("/budgets/{budget_id}")
def delete_budget(
    budget_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    budget = (
        db.query(BudgetEntry)
        .filter(BudgetEntry.id == budget_id, BudgetEntry.user_id == current_user.id)
        .first()
    )
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(budget)
    db.commit()
    return {"ok": True}


@app.get("/budgets/status", response_model=list[BudgetStatusItem])
def budget_status(
    year: int = Query(..., ge=1900, le=2100),
    month: int = Query(..., ge=1, le=12),
    currency: Literal["USD", "EUR", "RUB", "CNY"] = Query(default="USD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    budgets = (
        db.query(BudgetEntry)
        .filter(
            BudgetEntry.user_id == current_user.id,
            BudgetEntry.year == year,
            BudgetEntry.month == month,
            BudgetEntry.currency == currency,
        )
        .all()
    )

    spent_rows = (
        db.query(Transaction.category, func.sum(Transaction.amount).label("spent"))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.kind == "expense",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .group_by(Transaction.category)
        .all()
    )

    spent_by_category = {row.category: float(row.spent) for row in spent_rows}
    budget_by_category = {budget.category: float(budget.limit_amount) for budget in budgets}

    categories = sorted(set(spent_by_category.keys()) | set(budget_by_category.keys()))

    output = []
    for category in categories:
        spent = spent_by_category.get(category, 0.0)
        budget_amount = budget_by_category.get(category, 0.0)
        remaining = budget_amount - spent
        output.append(
            BudgetStatusItem(
                category=category,
                budget=budget_amount,
                spent=spent,
                remaining=remaining,
                over_limit=spent > budget_amount,
            )
        )

    return sorted(output, key=lambda item: item.remaining)
