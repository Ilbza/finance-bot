from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    first_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, default=0)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD", index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class BudgetEntry(Base):
    __tablename__ = "budget_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "year", "month", "currency", "category", name="uq_budget_entry_period_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD", index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    limit_amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class BotUserSettings(Base):
    __tablename__ = "bot_user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    default_period: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    cycle_start_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    limits_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reminders_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="off")
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class UserCategory(Base):
    __tablename__ = "user_categories"
    __table_args__ = (UniqueConstraint("user_id", "name_key", name="uq_user_category_name_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    name_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class QuestState(Base):
    __tablename__ = "quest_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    lesson: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=3000.0)
    savings: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strategy_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impulse_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goal: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    confidence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    future_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    credit_taken: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_reminder_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    last_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    last_action_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
