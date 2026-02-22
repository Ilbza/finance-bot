import csv
import io
import logging
import os
import re
from calendar import monthrange
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .database import Base, SessionLocal, engine
from .models import BotUserSettings, BudgetEntry, Transaction, User, UserCategory


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("finance-bot")


DEFAULT_EXPENSE_CATEGORIES = [
    "Еда",
    "Транспорт",
    "Кафе",
    "Дом",
    "Здоровье",
    "Развлечения",
    "Подписки",
    "Одежда",
    "Подарки",
    "Другое",
]
DEFAULT_CATEGORY_BY_KEY = {" ".join(item.lower().split()): item for item in DEFAULT_EXPENSE_CATEGORIES}
INCOME_TYPES = ["Зарплата", "Подработка", "Возврат", "Другое"]
MONTHS_RU = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]
CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥"}

MENU_ADD_EXPENSE = "➕ Добавить расход"
MENU_ADD_INCOME = "➕ Добавить доход (зарплата)"
MENU_SUMMARY = "📊 Итоги"
MENU_LIMITS = "🎯 Лимиты"
MENU_HISTORY = "🧾 История"
MENU_SETTINGS = "⚙️ Настройки"
MENU_CANCEL = "Отмена"
MENU_SKIP = "Пропустить"

STATE_IDLE = "idle"
STATE_ONBOARD_CURRENCY_OTHER = "onboarding_currency_other"
STATE_ONBOARD_LIMIT_AMOUNT = "onboarding_limit_amount"
STATE_ADD_EXPENSE_CATEGORY = "add_expense_category"
STATE_ADD_EXPENSE_CUSTOM_CATEGORY = "add_expense_custom_category"
STATE_ADD_EXPENSE_AMOUNT = "add_expense_amount"
STATE_ADD_EXPENSE_COMMENT = "add_expense_comment"
STATE_ADD_INCOME_TYPE = "add_income_type"
STATE_ADD_INCOME_AMOUNT = "add_income_amount"
STATE_ADD_INCOME_COMMENT = "add_income_comment"
STATE_LIMIT_SET_AMOUNT = "limit_set_amount"
STATE_HISTORY_EDIT_AMOUNT = "history_edit_amount"
STATE_SETTINGS_CURRENCY_OTHER = "settings_currency_other"
STATE_SETTINGS_CYCLE_DAY = "settings_cycle_day"
STATE_SUMMARY_PICK_PERIOD = "summary_pick_period"

STATE_KEY = "state"
STATE_DATA_KEY = "state_data"


def now_date() -> date:
    return datetime.utcnow().date()


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def current_year_month() -> tuple[int, int]:
    today = now_date()
    return today.year, today.month


def previous_year_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_title(year: int, month: int) -> str:
    return f"{MONTHS_RU[month - 1]} {year}"


def format_amount(value: float) -> str:
    rounded = round(float(value), 2)
    if abs(rounded - int(rounded)) < 1e-9:
        return f"{int(rounded):,}".replace(",", " ")
    return f"{rounded:,.2f}".replace(",", " ")


def format_money(value: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())
    return f"{format_amount(value)} {symbol}"


def normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def parse_amount(raw_value: str) -> Optional[float]:
    if not raw_value:
        return None

    cleaned = raw_value.strip()
    for char in ("₽", "$", "€", "¥"):
        cleaned = cleaned.replace(char, "")
    cleaned = re.sub(r"[рРrRuUbB]", "", cleaned)
    cleaned = cleaned.replace(" ", "")
    cleaned = re.sub(r"[^0-9,.\-+]", "", cleaned)
    if not cleaned:
        return None

    sign = 1
    if cleaned[0] in "+-":
        sign = -1 if cleaned[0] == "-" else 1
        cleaned = cleaned[1:]
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        last_comma = cleaned.rfind(",")
        last_dot = cleaned.rfind(".")
        decimal_sep = "," if last_comma > last_dot else "."
        thousand_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousand_sep, "")
        if decimal_sep == ",":
            cleaned = cleaned.replace(",", ".")
    elif "," in cleaned:
        if cleaned.count(",") == 1 and len(cleaned.split(",")[1]) in (1, 2):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned and cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")

    if cleaned.count(".") > 1:
        return None

    try:
        return float(cleaned) * sign
    except ValueError:
        return None


def parse_quick_expense(text: str) -> Optional[tuple[str, float]]:
    tokens = text.split()
    if len(tokens) < 2:
        return None

    max_parts = min(3, len(tokens) - 1)

    for parts in range(max_parts, 0, -1):
        amount = parse_amount(" ".join(tokens[-parts:]))
        if amount is not None and amount > 0:
            category = " ".join(tokens[:-parts]).strip()
            if category and re.search(r"[A-Za-zА-Яа-яЁё]", category):
                return category, amount

    for parts in range(max_parts, 0, -1):
        amount = parse_amount(" ".join(tokens[:parts]))
        if amount is not None and amount > 0:
            category = " ".join(tokens[parts:]).strip()
            if category and re.search(r"[A-Za-zА-Яа-яЁё]", category):
                return category, amount

    return None


def is_amount_only_text(text: str) -> bool:
    return bool(re.fullmatch(r"[\d\s,.\-+₽$€¥рРrRuUbB]+", text or ""))


def split_category_and_amount_tail(text: str) -> Optional[tuple[str, float]]:
    tokens = text.split()
    if len(tokens) < 2:
        return None

    max_parts = min(3, len(tokens) - 1)
    for parts in range(max_parts, 0, -1):
        amount = parse_amount(" ".join(tokens[-parts:]))
        if amount is not None and amount > 0:
            category = " ".join(tokens[:-parts]).strip()
            if category:
                return category, amount
    return None


def state_get(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get(STATE_KEY, STATE_IDLE)


def state_set(context: ContextTypes.DEFAULT_TYPE, state: str, data: Optional[dict] = None) -> None:
    context.user_data[STATE_KEY] = state
    if data is None:
        context.user_data.pop(STATE_DATA_KEY, None)
    else:
        context.user_data[STATE_DATA_KEY] = data


def state_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get(STATE_DATA_KEY, {})


def state_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[STATE_KEY] = STATE_IDLE
    context.user_data.pop(STATE_DATA_KEY, None)


def ensure_schema_compatibility() -> None:
    with engine.begin() as connection:
        txn_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(transactions)")).fetchall()}
        if "currency" not in txn_columns:
            connection.execute(text("ALTER TABLE transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'"))
        if "user_id" not in txn_columns:
            connection.execute(text("ALTER TABLE transactions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"))


def get_or_create_user(db: Session, telegram_user) -> User:
    user = db.query(User).filter(User.telegram_id == telegram_user.id).first()
    username = (telegram_user.username or "").strip()
    first_name = (telegram_user.first_name or "").strip()
    last_name = (telegram_user.last_name or "").strip()

    if user:
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        db.flush()
        return user

    user = User(
        telegram_id=telegram_user.id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )
    db.add(user)
    db.flush()
    return user


def get_or_create_settings(db: Session, user_id: int) -> BotUserSettings:
    settings = db.query(BotUserSettings).filter(BotUserSettings.user_id == user_id).first()
    if settings:
        return settings

    settings = BotUserSettings(user_id=user_id)
    db.add(settings)
    db.flush()
    return settings


def ensure_user_category(db: Session, user_id: int, category: str) -> str:
    clean = " ".join(category.strip().split())
    if not clean:
        return clean

    key = normalize_key(clean)
    if key in DEFAULT_CATEGORY_BY_KEY:
        return DEFAULT_CATEGORY_BY_KEY[key]

    existing = (
        db.query(UserCategory)
        .filter(UserCategory.user_id == user_id, UserCategory.name_key == key)
        .first()
    )
    if existing:
        return existing.name

    db.add(UserCategory(user_id=user_id, name=clean, name_key=key))
    db.flush()
    return clean


def list_expense_categories(db: Session, user_id: int) -> list[str]:
    output: list[str] = []
    seen_keys: set[str] = set()

    def _append(name: str) -> None:
        key = normalize_key(name)
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        output.append(name)

    for item in DEFAULT_EXPENSE_CATEGORIES:
        _append(item)

    user_rows = (
        db.query(UserCategory.name)
        .filter(UserCategory.user_id == user_id)
        .order_by(UserCategory.name.asc())
        .all()
    )
    for row in user_rows:
        if row.name:
            canonical = DEFAULT_CATEGORY_BY_KEY.get(normalize_key(row.name), row.name)
            _append(canonical)

    txn_rows = (
        db.query(Transaction.category)
        .filter(Transaction.user_id == user_id, Transaction.kind == "expense")
        .distinct()
        .all()
    )
    for row in txn_rows:
        if row.category:
            canonical = DEFAULT_CATEGORY_BY_KEY.get(normalize_key(row.category), row.category)
            _append(canonical)

    budget_rows = (
        db.query(BudgetEntry.category)
        .filter(BudgetEntry.user_id == user_id)
        .distinct()
        .all()
    )
    for row in budget_rows:
        if row.category:
            canonical = DEFAULT_CATEGORY_BY_KEY.get(normalize_key(row.category), row.category)
            _append(canonical)

    return output


def resolve_category(input_value: str, categories: list[str]) -> Optional[str]:
    key = normalize_key(input_value)
    for category in categories:
        if normalize_key(category) == key:
            return category
    if key in DEFAULT_CATEGORY_BY_KEY:
        return DEFAULT_CATEGORY_BY_KEY[key]
    return None


def create_transaction(
    db: Session,
    user_id: int,
    kind: str,
    amount: float,
    currency: str,
    category: str,
    note: str = "",
    txn_date: Optional[date] = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        kind=kind,
        amount=amount,
        currency=currency.upper(),
        category=category.strip(),
        txn_date=txn_date or now_date(),
        note=(note or "").strip(),
    )
    db.add(tx)
    db.flush()
    return tx


def month_totals(db: Session, user_id: int, currency: str, year: int, month: int) -> tuple[float, float, float]:
    start, end = month_bounds(year, month)
    income = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == user_id,
            Transaction.kind == "income",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .scalar()
        or 0.0
    )
    expense = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.user_id == user_id,
            Transaction.kind == "expense",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .scalar()
        or 0.0
    )
    return float(income), float(expense), float(income - expense)


def month_category_totals(
    db: Session,
    user_id: int,
    currency: str,
    year: int,
    month: int,
    kind: str = "expense",
) -> list[tuple[str, float]]:
    start, end = month_bounds(year, month)
    rows = (
        db.query(Transaction.category, func.sum(Transaction.amount).label("total"))
        .filter(
            Transaction.user_id == user_id,
            Transaction.kind == kind,
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
        .all()
    )
    return [(row.category, float(row.total)) for row in rows]


def month_day_totals(db: Session, user_id: int, currency: str, year: int, month: int) -> list[tuple[date, float]]:
    start, end = month_bounds(year, month)
    rows = (
        db.query(Transaction.txn_date, func.sum(Transaction.amount).label("total"))
        .filter(
            Transaction.user_id == user_id,
            Transaction.kind == "expense",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .group_by(Transaction.txn_date)
        .order_by(Transaction.txn_date.asc())
        .all()
    )
    return [(row.txn_date, float(row.total)) for row in rows]


def month_spent_by_category(db: Session, user_id: int, currency: str, year: int, month: int) -> dict[str, float]:
    start, end = month_bounds(year, month)
    rows = (
        db.query(Transaction.category, func.sum(Transaction.amount).label("spent"))
        .filter(
            Transaction.user_id == user_id,
            Transaction.kind == "expense",
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .group_by(Transaction.category)
        .all()
    )
    return {row.category: float(row.spent) for row in rows}


def month_budgets(db: Session, user_id: int, currency: str, year: int, month: int) -> dict[str, float]:
    rows = (
        db.query(BudgetEntry.category, BudgetEntry.limit_amount)
        .filter(
            BudgetEntry.user_id == user_id,
            BudgetEntry.currency == currency,
            BudgetEntry.year == year,
            BudgetEntry.month == month,
        )
        .all()
    )
    return {row.category: float(row.limit_amount) for row in rows}


def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_ADD_EXPENSE, MENU_ADD_INCOME],
            [MENU_SUMMARY, MENU_LIMITS],
            [MENU_HISTORY, MENU_SETTINGS],
        ],
        resize_keyboard=True,
    )


def build_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_CANCEL]], resize_keyboard=True, one_time_keyboard=True)


def build_skip_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_SKIP], [MENU_CANCEL]], resize_keyboard=True, one_time_keyboard=True)


def build_currency_inline(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("₽ RUB", callback_data=f"{prefix}:RUB"),
                InlineKeyboardButton("€ EUR", callback_data=f"{prefix}:EUR"),
            ],
            [
                InlineKeyboardButton("$ USD", callback_data=f"{prefix}:USD"),
                InlineKeyboardButton("¥ CNY", callback_data=f"{prefix}:CNY"),
            ],
            [InlineKeyboardButton("Другое", callback_data=f"{prefix}:OTHER")],
        ]
    )


def build_yes_no_inline(yes_data: str, no_data: str, yes_text: str = "Да", no_text: str = "Нет") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(yes_text, callback_data=yes_data), InlineKeyboardButton(no_text, callback_data=no_data)]]
    )


def build_expense_category_menu(categories: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[str]] = []
    chunk: list[str] = []
    for category in categories:
        chunk.append(category)
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append(["Другая", MENU_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def build_income_type_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["Зарплата", "Подработка"], ["Возврат", "Другое"], [MENU_CANCEL]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_summary_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Этот месяц", callback_data="summary:month:this"),
                InlineKeyboardButton("Прошлый", callback_data="summary:month:last"),
            ],
            [InlineKeyboardButton("Выбрать период", callback_data="summary:month:pick")],
            [
                InlineKeyboardButton("По категориям", callback_data=f"summary:cats:{year}:{month}"),
                InlineKeyboardButton("По дням", callback_data=f"summary:days:{year}:{month}"),
            ],
            [
                InlineKeyboardButton("Экспорт (CSV)", callback_data=f"summary:export:{year}:{month}"),
                InlineKeyboardButton("Поставить лимиты", callback_data="summary:limits"),
            ],
        ]
    )


def build_limits_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Установить лимит", callback_data="limits:set"),
            InlineKeyboardButton("✏️ Изменить лимит", callback_data="limits:edit"),
        ],
        [InlineKeyboardButton("🧹 Сбросить лимиты", callback_data="limits:reset")],
    ]
    if enabled:
        rows.append([InlineKeyboardButton("⛔️ Выключить лимиты", callback_data="limits:disable")])
    else:
        rows.append([InlineKeyboardButton("✅ Включить лимиты", callback_data="limits:enable")])
    return InlineKeyboardMarkup(rows)


def build_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Валюта", callback_data="settings:currency"),
                InlineKeyboardButton("Период", callback_data="settings:period"),
            ],
            [
                InlineKeyboardButton("День начала месяца", callback_data="settings:cycle"),
                InlineKeyboardButton("Напоминания", callback_data="settings:reminders"),
            ],
        ]
    )


def status_icon_for_percent(percent: float) -> str:
    if percent > 100:
        return "🚨"
    if percent >= 80:
        return "⚠️"
    return "✅"


def build_expense_feedback(
    db: Session,
    user_id: int,
    category: str,
    amount: float,
    currency: str,
    limits_enabled: bool,
    year: int,
    month: int,
) -> tuple[str, Optional[str]]:
    spent_by_category = month_spent_by_category(db, user_id, currency, year, month)
    budgets = month_budgets(db, user_id, currency, year, month)
    income_total, expense_total, balance = month_totals(db, user_id, currency, year, month)

    spent = float(spent_by_category.get(category, 0.0))
    limit = budgets.get(category)
    extra_notice: Optional[str] = None

    if limit and limit > 0:
        percent = (spent / limit) * 100
        icon = status_icon_for_percent(percent)
        limit_line = (
            f"{category}: {format_money(spent, currency)} / {format_money(limit, currency)} ({percent:.0f}%) {icon}"
        )
        if percent > 100:
            overflow = spent - limit
            extra_notice = f"🚨 Ты превысил(а) лимит «{category}» на {format_money(overflow, currency)}."
        elif percent >= 80:
            remaining = max(0.0, limit - spent)
            extra_notice = f"⚠️ {category} уже {percent:.0f}% от лимита. Осталось {format_money(remaining, currency)}."
    else:
        share = 0.0 if expense_total <= 0 else (spent / expense_total) * 100
        limit_line = f"{category}: {format_money(spent, currency)} ({share:.0f}% от расходов месяца)"

    if not limits_enabled:
        extra_notice = None

    text = (
        f"Записал(а): {category} — {format_money(amount, currency)} ✅\n"
        f"{limit_line}\n"
        f"Баланс месяца: {format_money(balance, currency)}"
    )
    _ = income_total
    return text, extra_notice


def build_income_feedback(
    db: Session,
    user_id: int,
    income_type: str,
    amount: float,
    currency: str,
    year: int,
    month: int,
) -> str:
    _, _, balance = month_totals(db, user_id, currency, year, month)
    return (
        f"Доход добавлен: {income_type} +{format_money(amount, currency)} ✅\n"
        f"Баланс месяца: {format_money(balance, currency)}"
    )


async def show_main_menu(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=build_main_menu())
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=build_main_menu())


async def send_summary(
    update: Update,
    db: Session,
    user_id: int,
    currency: str,
    year: int,
    month: int,
) -> None:
    income_total, expense_total, balance = month_totals(db, user_id, currency, year, month)
    top_categories = month_category_totals(db, user_id, currency, year, month, kind="expense")[:3]

    lines = [
        f"📊 Итоги за {month_title(year, month)}",
        f"Доходы: {format_money(income_total, currency)}",
        f"Расходы: {format_money(expense_total, currency)}",
        f"Баланс: {format_money(balance, currency)}",
        "",
        "Топ категорий:",
    ]
    if top_categories:
        for idx, (cat, total) in enumerate(top_categories, 1):
            lines.append(f"{idx}. {cat} — {format_money(total, currency)}")
    else:
        lines.append("Пока нет расходов в этом периоде.")

    text = "\n".join(lines)
    keyboard = build_summary_keyboard(year, month)
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)


async def send_limits_dashboard(update: Update, db: Session, user_id: int, settings: BotUserSettings) -> None:
    year, month = current_year_month()
    currency = settings.currency
    spent = month_spent_by_category(db, user_id, currency, year, month)
    budgets = month_budgets(db, user_id, currency, year, month)

    categories = sorted(set(spent.keys()) | set(budgets.keys()))
    lines = ["Лимиты на этот месяц:"]
    if categories:
        for category in categories:
            spent_value = float(spent.get(category, 0.0))
            budget_value = float(budgets.get(category, 0.0))
            if budget_value > 0:
                percent = (spent_value / budget_value) * 100
                icon = status_icon_for_percent(percent)
                lines.append(
                    f"{category}: {format_money(spent_value, currency)} / "
                    f"{format_money(budget_value, currency)} ({percent:.0f}%) {icon}"
                )
            else:
                lines.append(f"{category}: {format_money(spent_value, currency)} / —")
    else:
        lines.append("Пока лимиты не установлены.")

    text = "\n".join(lines)
    keyboard = build_limits_keyboard(settings.limits_enabled)

    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)


async def send_history(update: Update, db: Session, user_id: int, currency: str, offset: int = 0, limit: int = 5) -> None:
    rows = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id, Transaction.currency == currency)
        .order_by(Transaction.txn_date.desc(), Transaction.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not rows:
        text = "История пока пустая."
        if update.message:
            await update.message.reply_text(text, reply_markup=build_main_menu())
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(text, reply_markup=build_main_menu())
        return

    lines = ["Последние операции:"]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, tx in enumerate(rows, 1):
        sign = "-" if tx.kind == "expense" else "+"
        note_part = f" ({tx.note})" if tx.note else ""
        lines.append(f"{idx}. {tx.category} {sign}{format_money(tx.amount, currency)}{note_part}")
        keyboard_rows.append([InlineKeyboardButton(str(idx), callback_data=f"history:item:{tx.id}")])

    if len(rows) == limit:
        keyboard_rows.append([InlineKeyboardButton("Показать ещё", callback_data=f"history:more:{offset + limit}")])
    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)


def build_limit_picker_keyboard(context: ContextTypes.DEFAULT_TYPE, categories: list[str], key_name: str, cb_prefix: str):
    mapping = {str(idx): category for idx, category in enumerate(categories, 1)}
    context.user_data[key_name] = mapping
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, category in mapping.items():
        row.append(InlineKeyboardButton(category, callback_data=f"{cb_prefix}:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def summary_categories_text(db: Session, user_id: int, currency: str, year: int, month: int) -> str:
    categories = month_category_totals(db, user_id, currency, year, month, kind="expense")
    budgets = month_budgets(db, user_id, currency, year, month)
    if not categories:
        return "По категориям за период пока пусто."

    lines: list[str] = [f"По категориям за {month_title(year, month)}:"]
    for category, total in categories:
        limit = budgets.get(category)
        if limit and limit > 0:
            percent = total / limit * 100
            icon = status_icon_for_percent(percent)
            lines.append(
                f"{category}: {format_money(total, currency)} / {format_money(limit, currency)} ({percent:.0f}%) {icon}"
            )
        else:
            lines.append(f"{category}: {format_money(total, currency)}")
    return "\n".join(lines)


def summary_days_text(db: Session, user_id: int, currency: str, year: int, month: int) -> str:
    totals = month_day_totals(db, user_id, currency, year, month)
    if not totals:
        return "По дням за период пока пусто."

    lines = [f"По дням за {month_title(year, month)}:"]
    for day, total in totals[-10:]:
        lines.append(f"{day.strftime('%d.%m')}: {format_money(total, currency)}")
    return "\n".join(lines)


async def send_export_csv(update: Update, db: Session, user_id: int, currency: str, year: int, month: int) -> None:
    start, end = month_bounds(year, month)
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.currency == currency,
            Transaction.txn_date >= start,
            Transaction.txn_date <= end,
        )
        .order_by(Transaction.txn_date.asc(), Transaction.id.asc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "date", "kind", "currency", "category", "amount", "note", "created_at"])
    for tx in rows:
        writer.writerow(
            [
                tx.id,
                tx.txn_date.isoformat(),
                tx.kind,
                tx.currency,
                tx.category,
                tx.amount,
                tx.note,
                tx.created_at.isoformat(),
            ]
        )
    payload = io.BytesIO(buffer.getvalue().encode("utf-8"))
    payload.seek(0)

    filename = f"finance-{year}-{month:02d}.csv"
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_document(
            document=InputFile(payload, filename=filename),
            caption=f"Экспорт за {month_title(year, month)}",
        )
    elif update.message:
        await update.message.reply_document(
            document=InputFile(payload, filename=filename),
            caption=f"Экспорт за {month_title(year, month)}",
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    state_clear(context)
    with SessionLocal() as db:
        user = get_or_create_user(db, update.effective_user)
        settings = get_or_create_settings(db, user.id)
        db.commit()

        if settings.onboarding_completed:
            await update.message.reply_text(
                "С возвращением. Можешь писать вроде: еда 450 или зп 250000.",
                reply_markup=build_main_menu(),
            )
            return

    await update.message.reply_text(
        "Привет! Я помогу вести бюджет без табличек 😌\n"
        "Как будем считать: по текущему месяцу. Валюта?",
        reply_markup=build_currency_inline("onboarding:currency"),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_clear(context)
    if not update.message:
        return
    await update.message.reply_text(
        "Быстрые примеры:\n"
        "• еда 350\n"
        "• 350 еда\n"
        "• зп 250000\n"
        "• лимит еда 30000\n"
        "• итоги / баланс / остаток\n\n"
        "Для выхода из шага нажми «Отмена».",
        reply_markup=build_main_menu(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_clear(context)
    if update.message:
        await update.message.reply_text("Главное меню:", reply_markup=build_main_menu())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_clear(context)
    context.user_data.pop("pending_unknown_category", None)
    context.user_data.pop("pending_amount_only", None)
    if update.message:
        await update.message.reply_text("Ок, отменил.", reply_markup=build_main_menu())


async def finalize_onboarding(
    query_or_update: Update,
    settings: BotUserSettings,
    db: Session,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    settings.onboarding_completed = True
    db.commit()
    state_clear(context)
    if query_or_update.callback_query and query_or_update.callback_query.message:
        await query_or_update.callback_query.message.reply_text(
            "Супер. Теперь просто пиши: еда 450 или жми кнопки меню.",
            reply_markup=build_main_menu(),
        )
    elif query_or_update.message:
        await query_or_update.message.reply_text(
            "Супер. Теперь просто пиши: еда 450 или жми кнопки меню.",
            reply_markup=build_main_menu(),
        )


async def handle_state_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Session,
    user: User,
    settings: BotUserSettings,
    text: str,
) -> bool:
    current_state = state_get(context)
    if current_state == STATE_IDLE:
        return False

    if text == MENU_CANCEL:
        state_clear(context)
        await update.message.reply_text("Ок, отменил.", reply_markup=build_main_menu())
        return True

    if current_state == STATE_ONBOARD_CURRENCY_OTHER:
        currency = normalize_key(text).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            await update.message.reply_text("Нужен 3-буквенный код валюты. Например: GBP")
            return True
        settings.currency = currency
        db.commit()
        state_clear(context)
        await update.message.reply_text(
            "Ок. Хочешь включить лимиты по категориям? (можно позже)",
            reply_markup=build_yes_no_inline("onboarding:limits:yes", "onboarding:limits:no", "Да, настроить", "Пока нет"),
        )
        return True

    if current_state == STATE_ONBOARD_LIMIT_AMOUNT:
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        data = state_data(context)
        queue = data.get("queue", [])
        current_category = data.get("current")
        year, month = current_year_month()

        if current_category:
            existing = (
                db.query(BudgetEntry)
                .filter(
                    BudgetEntry.user_id == user.id,
                    BudgetEntry.year == year,
                    BudgetEntry.month == month,
                    BudgetEntry.currency == settings.currency,
                    BudgetEntry.category == current_category,
                )
                .first()
            )
            if existing:
                existing.limit_amount = amount
            else:
                db.add(
                    BudgetEntry(
                        user_id=user.id,
                        year=year,
                        month=month,
                        currency=settings.currency,
                        category=current_category,
                        limit_amount=amount,
                    )
                )
            db.flush()
            await update.message.reply_text(f"Готово: {current_category} — {format_money(amount, settings.currency)} ✅")

        if queue:
            next_category = queue.pop(0)
            state_set(context, STATE_ONBOARD_LIMIT_AMOUNT, {"queue": queue, "current": next_category})
            db.commit()
            await update.message.reply_text(
                f"Лимит для «{next_category}» на месяц?",
                reply_markup=build_cancel_menu(),
            )
            return True

        settings.limits_enabled = True
        settings.onboarding_completed = True
        db.commit()
        state_clear(context)
        await update.message.reply_text(
            "Супер. Теперь просто пиши: еда 450 или жми кнопки меню.",
            reply_markup=build_main_menu(),
        )
        return True

    if current_state == STATE_ADD_EXPENSE_CATEGORY:
        categories = list_expense_categories(db, user.id)
        if text == "Другая":
            state_set(context, STATE_ADD_EXPENSE_CUSTOM_CATEGORY)
            await update.message.reply_text("Напиши название категории:", reply_markup=build_cancel_menu())
            return True
        category = resolve_category(text, categories)
        if not category:
            await update.message.reply_text("Не вижу такую категорию. Выбери из списка или нажми «Другая».")
            return True
        state_set(context, STATE_ADD_EXPENSE_AMOUNT, {"category": category})
        await update.message.reply_text("Сколько потратил(а)?", reply_markup=build_cancel_menu())
        return True

    if current_state == STATE_ADD_EXPENSE_CUSTOM_CATEGORY:
        category = " ".join(text.split())
        if len(category) < 2 or len(category) > 64:
            await update.message.reply_text("Название категории должно быть от 2 до 64 символов.")
            return True
        category = ensure_user_category(db, user.id, category)
        db.commit()
        state_set(context, STATE_ADD_EXPENSE_AMOUNT, {"category": category})
        await update.message.reply_text("Сколько потратил(а)?", reply_markup=build_cancel_menu())
        return True

    if current_state == STATE_ADD_EXPENSE_AMOUNT:
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        data = state_data(context)
        state_set(context, STATE_ADD_EXPENSE_COMMENT, {"category": data.get("category"), "amount": amount})
        await update.message.reply_text("Комментарий? (необязательно)", reply_markup=build_skip_menu())
        return True

    if current_state == STATE_ADD_EXPENSE_COMMENT:
        note = "" if text == MENU_SKIP else text
        data = state_data(context)
        category = data.get("category")
        amount = float(data.get("amount", 0))
        if not category or amount <= 0:
            state_clear(context)
            await update.message.reply_text("Что-то пошло не так, давай заново.", reply_markup=build_main_menu())
            return True
        category = ensure_user_category(db, user.id, category)
        create_transaction(
            db=db,
            user_id=user.id,
            kind="expense",
            amount=amount,
            currency=settings.currency,
            category=category,
            note=note,
        )
        year, month = current_year_month()
        db.commit()
        text_main, extra = build_expense_feedback(
            db=db,
            user_id=user.id,
            category=category,
            amount=amount,
            currency=settings.currency,
            limits_enabled=settings.limits_enabled,
            year=year,
            month=month,
        )
        state_clear(context)
        await update.message.reply_text(text_main, reply_markup=build_main_menu())
        if extra:
            context.user_data["limit_alert_category"] = category
            await update.message.reply_text(
                f"{extra}\nЧто делаем?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Увеличить лимит", callback_data="limit:raise")],
                        [InlineKeyboardButton("Оставить как есть", callback_data="limit:keep")],
                        [InlineKeyboardButton("Перенести в «Другое»", callback_data="limit:move_other")],
                    ]
                ),
            )
        return True

    if current_state == STATE_ADD_INCOME_TYPE:
        if text not in INCOME_TYPES:
            await update.message.reply_text("Выбери тип дохода из кнопок.")
            return True
        state_set(context, STATE_ADD_INCOME_AMOUNT, {"income_type": text})
        await update.message.reply_text("Сумма?", reply_markup=build_cancel_menu())
        return True

    if current_state == STATE_ADD_INCOME_AMOUNT:
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        data = state_data(context)
        state_set(context, STATE_ADD_INCOME_COMMENT, {"income_type": data.get("income_type"), "amount": amount})
        await update.message.reply_text("Комментарий? (необязательно)", reply_markup=build_skip_menu())
        return True

    if current_state == STATE_ADD_INCOME_COMMENT:
        note = "" if text == MENU_SKIP else text
        data = state_data(context)
        income_type = data.get("income_type") or "Доход"
        amount = float(data.get("amount", 0))
        if amount <= 0:
            state_clear(context)
            await update.message.reply_text("Что-то пошло не так, давай заново.", reply_markup=build_main_menu())
            return True
        create_transaction(
            db=db,
            user_id=user.id,
            kind="income",
            amount=amount,
            currency=settings.currency,
            category=income_type,
            note=note,
        )
        year, month = current_year_month()
        db.commit()
        response = build_income_feedback(db, user.id, income_type, amount, settings.currency, year, month)
        state_clear(context)
        await update.message.reply_text(response, reply_markup=build_main_menu())
        return True

    if current_state == STATE_LIMIT_SET_AMOUNT:
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        data = state_data(context)
        category = data.get("category")
        if not category:
            state_clear(context)
            await update.message.reply_text("Не удалось определить категорию.", reply_markup=build_main_menu())
            return True
        year, month = current_year_month()
        existing = (
            db.query(BudgetEntry)
            .filter(
                BudgetEntry.user_id == user.id,
                BudgetEntry.year == year,
                BudgetEntry.month == month,
                BudgetEntry.currency == settings.currency,
                BudgetEntry.category == category,
            )
            .first()
        )
        if existing:
            existing.limit_amount = amount
        else:
            db.add(
                BudgetEntry(
                    user_id=user.id,
                    year=year,
                    month=month,
                    currency=settings.currency,
                    category=category,
                    limit_amount=amount,
                )
            )
        settings.limits_enabled = True
        db.commit()
        state_clear(context)
        context.user_data.pop("limit_alert_category", None)
        await update.message.reply_text(
            f"Готово ✅\n{category}: {format_money(amount, settings.currency)}",
            reply_markup=build_main_menu(),
        )
        return True

    if current_state == STATE_HISTORY_EDIT_AMOUNT:
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        data = state_data(context)
        tx_id = int(data.get("tx_id", 0))
        tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
        if not tx:
            state_clear(context)
            await update.message.reply_text("Операция не найдена.", reply_markup=build_main_menu())
            return True
        tx.amount = amount
        db.commit()
        state_clear(context)
        await update.message.reply_text("Сумма обновлена ✅", reply_markup=build_main_menu())
        return True

    if current_state == STATE_SETTINGS_CURRENCY_OTHER:
        currency = normalize_key(text).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            await update.message.reply_text("Нужен 3-буквенный код валюты. Например: GBP")
            return True
        settings.currency = currency
        db.commit()
        state_clear(context)
        await update.message.reply_text(f"Валюта обновлена: {currency} ✅", reply_markup=build_main_menu())
        return True

    if current_state == STATE_SETTINGS_CYCLE_DAY:
        if not text.isdigit():
            await update.message.reply_text("Введи число от 1 до 31.")
            return True
        day = int(text)
        if day < 1 or day > 31:
            await update.message.reply_text("Нужно число от 1 до 31.")
            return True
        settings.cycle_start_day = day
        db.commit()
        state_clear(context)
        await update.message.reply_text(f"День начала цикла: {day} ✅", reply_markup=build_main_menu())
        return True

    if current_state == STATE_SUMMARY_PICK_PERIOD:
        period_raw = text.strip()
        match = re.fullmatch(r"(0?[1-9]|1[0-2])[./-](20\d{2})", period_raw)
        if not match:
            await update.message.reply_text("Формат периода: MM.YYYY, например 02.2026")
            return True
        month = int(match.group(1))
        year = int(match.group(2))
        context.user_data["summary_period"] = (year, month)
        state_clear(context)
        await send_summary(update, db, user.id, settings.currency, year, month)
        return True

    return False


async def handle_quick_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Session,
    user: User,
    settings: BotUserSettings,
    text: str,
) -> bool:
    normalized = normalize_key(text)

    if normalized in {"итоги", "баланс", "остаток"}:
        year, month = current_year_month()
        context.user_data["summary_period"] = (year, month)
        await send_summary(update, db, user.id, settings.currency, year, month)
        return True

    if normalized.startswith("лимит "):
        payload = text.strip()[5:].strip()
        parsed = split_category_and_amount_tail(payload)
        if not parsed:
            await update.message.reply_text("Формат: лимит <категория> <сумма>")
            return True
        category_raw, amount = parsed
        categories = list_expense_categories(db, user.id)
        category = resolve_category(category_raw, categories)
        if not category:
            category = ensure_user_category(db, user.id, category_raw)
        year, month = current_year_month()
        existing = (
            db.query(BudgetEntry)
            .filter(
                BudgetEntry.user_id == user.id,
                BudgetEntry.year == year,
                BudgetEntry.month == month,
                BudgetEntry.currency == settings.currency,
                BudgetEntry.category == category,
            )
            .first()
        )
        if existing:
            existing.limit_amount = amount
        else:
            db.add(
                BudgetEntry(
                    user_id=user.id,
                    year=year,
                    month=month,
                    currency=settings.currency,
                    category=category,
                    limit_amount=amount,
                )
            )
        settings.limits_enabled = True
        db.commit()
        await update.message.reply_text(
            f"Лимит установлен ✅\n{category}: {format_money(amount, settings.currency)}",
            reply_markup=build_main_menu(),
        )
        return True

    income_match = re.match(r"^(зп|зарплата|доход)\s+(.+)$", normalized)
    if income_match:
        amount_raw = text.split(maxsplit=1)[1]
        amount = parse_amount(amount_raw)
        if amount is None or amount <= 0:
            await update.message.reply_text("Нужна корректная сумма больше 0.")
            return True
        income_type = "Зарплата" if income_match.group(1) in {"зп", "зарплата"} else "Доход"
        create_transaction(
            db=db,
            user_id=user.id,
            kind="income",
            amount=amount,
            currency=settings.currency,
            category=income_type,
            note="",
        )
        year, month = current_year_month()
        db.commit()
        response = build_income_feedback(db, user.id, income_type, amount, settings.currency, year, month)
        await update.message.reply_text(response, reply_markup=build_main_menu())
        return True

    parsed_expense = parse_quick_expense(text)
    if parsed_expense:
        category_raw, amount = parsed_expense
        categories = list_expense_categories(db, user.id)
        category = resolve_category(category_raw, categories)
        if not category:
            context.user_data["pending_unknown_category"] = {"name": category_raw, "amount": amount}
            await update.message.reply_text(
                f"Категория «{category_raw}» новая. Создать?",
                reply_markup=build_yes_no_inline("cat_new:yes", "cat_new:no", "Да", "Нет, выбрать из списка"),
            )
            return True

        category = ensure_user_category(db, user.id, category)
        create_transaction(
            db=db,
            user_id=user.id,
            kind="expense",
            amount=amount,
            currency=settings.currency,
            category=category,
            note="",
        )
        year, month = current_year_month()
        db.commit()
        main_text, extra = build_expense_feedback(
            db=db,
            user_id=user.id,
            category=category,
            amount=amount,
            currency=settings.currency,
            limits_enabled=settings.limits_enabled,
            year=year,
            month=month,
        )
        await update.message.reply_text(main_text, reply_markup=build_main_menu())
        if extra:
            await update.message.reply_text(extra)
        return True

    if is_amount_only_text(text):
        amount_only = parse_amount(text)
        if amount_only is None:
            return False
        if amount_only <= 0:
            await update.message.reply_text("Нужна сумма больше 0.")
            return True
        context.user_data["pending_amount_only"] = float(amount_only)
        await update.message.reply_text(
            "Это расход или доход?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Расход", callback_data="amount_only:expense"),
                        InlineKeyboardButton("Доход", callback_data="amount_only:income"),
                    ]
                ]
            ),
        )
        return True

    return False


async def handle_menu_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Session,
    user: User,
    settings: BotUserSettings,
    text: str,
) -> bool:
    if text == MENU_ADD_EXPENSE:
        categories = list_expense_categories(db, user.id)
        state_set(context, STATE_ADD_EXPENSE_CATEGORY)
        await update.message.reply_text("Выбери категорию:", reply_markup=build_expense_category_menu(categories))
        return True

    if text == MENU_ADD_INCOME:
        state_set(context, STATE_ADD_INCOME_TYPE)
        await update.message.reply_text("Выбери тип дохода:", reply_markup=build_income_type_menu())
        return True

    if text == MENU_SUMMARY:
        year, month = current_year_month()
        context.user_data["summary_period"] = (year, month)
        await send_summary(update, db, user.id, settings.currency, year, month)
        return True

    if text == MENU_LIMITS:
        await send_limits_dashboard(update, db, user.id, settings)
        return True

    if text == MENU_HISTORY:
        await send_history(update, db, user.id, settings.currency)
        return True

    if text == MENU_SETTINGS:
        await update.message.reply_text(
            "Настройки:\n"
            f"Валюта: {settings.currency}\n"
            f"Период: {settings.default_period}\n"
            f"День начала цикла: {settings.cycle_start_day or '1'}\n"
            f"Напоминания: {settings.reminders_mode}",
            reply_markup=build_settings_keyboard(),
        )
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    text = update.message.text.strip()
    if not text:
        return

    if text in {MENU_ADD_EXPENSE, MENU_ADD_INCOME, MENU_SUMMARY, MENU_LIMITS, MENU_HISTORY, MENU_SETTINGS}:
        state_clear(context)

    with SessionLocal() as db:
        user = get_or_create_user(db, update.effective_user)
        settings = get_or_create_settings(db, user.id)
        db.commit()

        if await handle_state_text(update, context, db, user, settings, text):
            db.commit()
            return

        if await handle_menu_action(update, context, db, user, settings, text):
            db.commit()
            return

        if await handle_quick_commands(update, context, db, user, settings, text):
            db.commit()
            return

        await update.message.reply_text(
            "Не понял команду. Пример: еда 450, зп 250000, лимит еда 30000, итоги.",
            reply_markup=build_main_menu(),
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()
    data = query.data or ""

    with SessionLocal() as db:
        user = get_or_create_user(db, update.effective_user)
        settings = get_or_create_settings(db, user.id)
        db.commit()

        if data.startswith("onboarding:currency:"):
            code = data.split(":")[-1]
            if code == "OTHER":
                state_set(context, STATE_ONBOARD_CURRENCY_OTHER)
                await query.message.reply_text("Введи 3-буквенный код валюты (например, GBP):")
                return
            settings.currency = code
            db.commit()
            await query.message.reply_text(
                "Ок. Хочешь включить лимиты по категориям? (можно позже)",
                reply_markup=build_yes_no_inline("onboarding:limits:yes", "onboarding:limits:no", "Да, настроить", "Пока нет"),
            )
            return

        if data == "onboarding:limits:no":
            settings.limits_enabled = False
            await finalize_onboarding(update, settings, db, context)
            return

        if data == "onboarding:limits:yes":
            context.user_data["onboarding_limit_selected"] = []
            rows = []
            for idx, category in enumerate(DEFAULT_EXPENSE_CATEGORIES, 1):
                rows.append([InlineKeyboardButton(category, callback_data=f"onboarding:pick:{idx}")])
            rows.append([InlineKeyboardButton("Готово", callback_data="onboarding:pick:done")])
            await query.message.reply_text(
                "Выбери категории, на которые хочешь поставить лимиты.",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("onboarding:pick:"):
            token = data.split(":")[-1]
            selected = context.user_data.get("onboarding_limit_selected", [])
            if token == "done":
                if not selected:
                    settings.limits_enabled = True
                    await finalize_onboarding(update, settings, db, context)
                    return
                queue = selected[:]
                current = queue.pop(0)
                state_set(context, STATE_ONBOARD_LIMIT_AMOUNT, {"queue": queue, "current": current})
                await query.message.reply_text(f"Лимит для «{current}» на месяц?", reply_markup=build_cancel_menu())
                return
            idx = int(token) - 1
            if idx < 0 or idx >= len(DEFAULT_EXPENSE_CATEGORIES):
                return
            category = DEFAULT_EXPENSE_CATEGORIES[idx]
            if category in selected:
                selected.remove(category)
            else:
                selected.append(category)
            context.user_data["onboarding_limit_selected"] = selected
            picked = ", ".join(selected) if selected else "пока ничего"
            await query.message.reply_text(f"Выбрано: {picked}")
            return

        if data == "amount_only:expense":
            amount = context.user_data.get("pending_amount_only")
            if not amount:
                await query.message.reply_text("Сумма не найдена, введи ещё раз.")
                return
            categories = list_expense_categories(db, user.id)
            keyboard = build_limit_picker_keyboard(context, categories, "amount_only_categories", "amount_only:cat")
            await query.message.reply_text("В какую категорию записать расход?", reply_markup=keyboard)
            return

        if data == "amount_only:income":
            amount = context.user_data.get("pending_amount_only")
            if not amount:
                await query.message.reply_text("Сумма не найдена, введи ещё раз.")
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Зарплата", callback_data="amount_only:income_type:Зарплата")],
                    [InlineKeyboardButton("Подработка", callback_data="amount_only:income_type:Подработка")],
                    [InlineKeyboardButton("Возврат", callback_data="amount_only:income_type:Возврат")],
                    [InlineKeyboardButton("Другое", callback_data="amount_only:income_type:Доход")],
                ]
            )
            await query.message.reply_text("Выбери тип дохода:", reply_markup=keyboard)
            return

        if data.startswith("amount_only:cat:"):
            idx = data.split(":")[-1]
            mapping = context.user_data.get("amount_only_categories", {})
            category = mapping.get(idx)
            amount = context.user_data.get("pending_amount_only")
            if not category or not amount:
                await query.message.reply_text("Не удалось завершить операцию, введи сумму ещё раз.")
                return
            category = ensure_user_category(db, user.id, category)
            create_transaction(
                db=db,
                user_id=user.id,
                kind="expense",
                amount=float(amount),
                currency=settings.currency,
                category=category,
                note="",
            )
            year, month = current_year_month()
            db.commit()
            context.user_data.pop("pending_amount_only", None)
            text_main, extra = build_expense_feedback(
                db=db,
                user_id=user.id,
                category=category,
                amount=float(amount),
                currency=settings.currency,
                limits_enabled=settings.limits_enabled,
                year=year,
                month=month,
            )
            await query.message.reply_text(text_main, reply_markup=build_main_menu())
            if extra:
                await query.message.reply_text(extra)
            return

        if data.startswith("amount_only:income_type:"):
            income_type = data.split(":", 3)[-1]
            amount = context.user_data.get("pending_amount_only")
            if not amount:
                await query.message.reply_text("Не удалось завершить операцию, введи сумму ещё раз.")
                return
            create_transaction(
                db=db,
                user_id=user.id,
                kind="income",
                amount=float(amount),
                currency=settings.currency,
                category=income_type,
                note="",
            )
            year, month = current_year_month()
            db.commit()
            context.user_data.pop("pending_amount_only", None)
            response = build_income_feedback(db, user.id, income_type, float(amount), settings.currency, year, month)
            await query.message.reply_text(response, reply_markup=build_main_menu())
            return

        if data == "cat_new:yes":
            pending = context.user_data.get("pending_unknown_category")
            if not pending:
                await query.message.reply_text("Запрос устарел. Введи операцию снова.")
                return
            category = ensure_user_category(db, user.id, pending["name"])
            amount = float(pending["amount"])
            create_transaction(
                db=db,
                user_id=user.id,
                kind="expense",
                amount=amount,
                currency=settings.currency,
                category=category,
                note="",
            )
            year, month = current_year_month()
            db.commit()
            context.user_data.pop("pending_unknown_category", None)
            text_main, extra = build_expense_feedback(
                db=db,
                user_id=user.id,
                category=category,
                amount=amount,
                currency=settings.currency,
                limits_enabled=settings.limits_enabled,
                year=year,
                month=month,
            )
            await query.message.reply_text(text_main, reply_markup=build_main_menu())
            if extra:
                await query.message.reply_text(extra)
            return

        if data == "cat_new:no":
            pending = context.user_data.get("pending_unknown_category")
            if not pending:
                await query.message.reply_text("Запрос устарел. Введи операцию снова.")
                return
            categories = list_expense_categories(db, user.id)
            keyboard = build_limit_picker_keyboard(context, categories, "cat_replace_choices", "cat_replace")
            await query.message.reply_text("Выбери категорию из списка:", reply_markup=keyboard)
            return

        if data.startswith("cat_replace:"):
            idx = data.split(":")[-1]
            category = context.user_data.get("cat_replace_choices", {}).get(idx)
            pending = context.user_data.get("pending_unknown_category")
            if not pending or not category:
                await query.message.reply_text("Запрос устарел. Введи операцию снова.")
                return
            amount = float(pending["amount"])
            create_transaction(
                db=db,
                user_id=user.id,
                kind="expense",
                amount=amount,
                currency=settings.currency,
                category=category,
                note="",
            )
            year, month = current_year_month()
            db.commit()
            context.user_data.pop("pending_unknown_category", None)
            text_main, extra = build_expense_feedback(
                db=db,
                user_id=user.id,
                category=category,
                amount=amount,
                currency=settings.currency,
                limits_enabled=settings.limits_enabled,
                year=year,
                month=month,
            )
            await query.message.reply_text(text_main, reply_markup=build_main_menu())
            if extra:
                await query.message.reply_text(extra)
            return

        if data.startswith("summary:month:"):
            token = data.split(":")[-1]
            year, month = current_year_month()
            if token == "last":
                year, month = previous_year_month(year, month)
            elif token == "pick":
                state_set(context, STATE_SUMMARY_PICK_PERIOD)
                await query.message.reply_text("Введи период в формате MM.YYYY, например 02.2026")
                return
            context.user_data["summary_period"] = (year, month)
            await send_summary(update, db, user.id, settings.currency, year, month)
            return

        if data.startswith("summary:cats:"):
            _, _, y_raw, m_raw = data.split(":")
            text_response = summary_categories_text(db, user.id, settings.currency, int(y_raw), int(m_raw))
            await query.message.reply_text(text_response)
            return

        if data.startswith("summary:days:"):
            _, _, y_raw, m_raw = data.split(":")
            text_response = summary_days_text(db, user.id, settings.currency, int(y_raw), int(m_raw))
            await query.message.reply_text(text_response)
            return

        if data.startswith("summary:export:"):
            _, _, y_raw, m_raw = data.split(":")
            await send_export_csv(update, db, user.id, settings.currency, int(y_raw), int(m_raw))
            return

        if data == "summary:limits":
            await send_limits_dashboard(update, db, user.id, settings)
            return

        if data.startswith("limits:"):
            action = data.split(":")[1]
            year, month = current_year_month()

            if action in {"set", "edit"}:
                categories = list_expense_categories(db, user.id)
                keyboard = build_limit_picker_keyboard(context, categories, "limits_category_choices", "limits:cat")
                await query.message.reply_text("На какую категорию поставить лимит?", reply_markup=keyboard)
                return

            if action == "reset":
                (
                    db.query(BudgetEntry)
                    .filter(
                        BudgetEntry.user_id == user.id,
                        BudgetEntry.year == year,
                        BudgetEntry.month == month,
                        BudgetEntry.currency == settings.currency,
                    )
                    .delete()
                )
                db.commit()
                await query.message.reply_text("Лимиты очищены ✅", reply_markup=build_main_menu())
                return

            if action == "disable":
                settings.limits_enabled = False
                db.commit()
                await query.message.reply_text("Лимиты выключены ✅", reply_markup=build_main_menu())
                return

            if action == "enable":
                settings.limits_enabled = True
                db.commit()
                await query.message.reply_text("Лимиты включены ✅", reply_markup=build_main_menu())
                return

        if data.startswith("limits:cat:"):
            idx = data.split(":")[-1]
            category = context.user_data.get("limits_category_choices", {}).get(idx)
            if not category:
                await query.message.reply_text("Категория не найдена, попробуй ещё раз.")
                return
            state_set(context, STATE_LIMIT_SET_AMOUNT, {"category": category})
            await query.message.reply_text(f"Какой лимит для «{category}»?", reply_markup=build_cancel_menu())
            return

        if data.startswith("history:more:"):
            offset = int(data.split(":")[-1])
            await send_history(update, db, user.id, settings.currency, offset=offset)
            return

        if data.startswith("history:item:"):
            tx_id = int(data.split(":")[-1])
            tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
            if not tx:
                await query.message.reply_text("Операция не найдена.")
                return
            sign = "-" if tx.kind == "expense" else "+"
            note_part = f"\nКомментарий: {tx.note}" if tx.note else ""
            text_msg = (
                f"Операция #{tx.id}\n"
                f"{tx.txn_date.isoformat()} • {tx.category} • {sign}{format_money(tx.amount, tx.currency)}"
                f"{note_part}\n"
                "Что сделать?"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ Изменить сумму", callback_data=f"history:edit_amount:{tx.id}")],
                    [InlineKeyboardButton("🔁 Изменить категорию", callback_data=f"history:edit_category:{tx.id}")],
                    [InlineKeyboardButton("🗑 Удалить", callback_data=f"history:delete:{tx.id}")],
                    [InlineKeyboardButton("Назад", callback_data="history:back")],
                ]
            )
            await query.message.reply_text(text_msg, reply_markup=keyboard)
            return

        if data == "history:back":
            await send_history(update, db, user.id, settings.currency)
            return

        if data.startswith("history:edit_amount:"):
            tx_id = int(data.split(":")[-1])
            tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
            if not tx:
                await query.message.reply_text("Операция не найдена.")
                return
            state_set(context, STATE_HISTORY_EDIT_AMOUNT, {"tx_id": tx_id})
            await query.message.reply_text("Введи новую сумму:", reply_markup=build_cancel_menu())
            return

        if data.startswith("history:edit_category:"):
            tx_id = int(data.split(":")[-1])
            tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
            if not tx:
                await query.message.reply_text("Операция не найдена.")
                return
            categories = list_expense_categories(db, user.id)
            keyboard = build_limit_picker_keyboard(context, categories, "history_category_choices", f"history:set_category:{tx_id}")
            await query.message.reply_text("Выбери новую категорию:", reply_markup=keyboard)
            return

        if data.startswith("history:set_category:"):
            parts = data.split(":")
            tx_id = int(parts[2])
            idx = parts[3]
            category = context.user_data.get("history_category_choices", {}).get(idx)
            tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
            if not tx or not category:
                await query.message.reply_text("Не удалось изменить категорию.")
                return
            tx.category = category
            db.commit()
            await query.message.reply_text("Категория обновлена ✅", reply_markup=build_main_menu())
            return

        if data.startswith("history:delete:"):
            tx_id = int(data.split(":")[-1])
            tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == user.id).first()
            if not tx:
                await query.message.reply_text("Операция не найдена.")
                return
            db.delete(tx)
            db.commit()
            await query.message.reply_text("Операция удалена ✅", reply_markup=build_main_menu())
            return

        if data.startswith("settings:"):
            action = data.split(":")[1]

            if action == "currency":
                await query.message.reply_text(
                    "Выбери валюту:",
                    reply_markup=build_currency_inline("settings:currency_value"),
                )
                return

            if action == "currency_value":
                code = data.split(":")[-1]
                if code == "OTHER":
                    state_set(context, STATE_SETTINGS_CURRENCY_OTHER)
                    await query.message.reply_text("Введи 3-буквенный код валюты (например, GBP):")
                    return
                settings.currency = code
                db.commit()
                await query.message.reply_text(f"Валюта обновлена: {code} ✅", reply_markup=build_main_menu())
                return

            if action == "period":
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Месяц", callback_data="settings:period_value:month")],
                        [InlineKeyboardButton("Неделя", callback_data="settings:period_value:week")],
                    ]
                )
                await query.message.reply_text("Период по умолчанию:", reply_markup=keyboard)
                return

            if action == "period_value":
                value = data.split(":")[-1]
                settings.default_period = "week" if value == "week" else "month"
                db.commit()
                await query.message.reply_text(
                    f"Период обновлён: {settings.default_period} ✅",
                    reply_markup=build_main_menu(),
                )
                return

            if action == "cycle":
                state_set(context, STATE_SETTINGS_CYCLE_DAY)
                await query.message.reply_text("Введи день начала месяца (1-31):", reply_markup=build_cancel_menu())
                return

            if action == "reminders":
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Каждый день 21:00", callback_data="settings:rem:daily")],
                        [InlineKeyboardButton("По будням", callback_data="settings:rem:weekdays")],
                        [InlineKeyboardButton("Раз в неделю", callback_data="settings:rem:weekly")],
                        [InlineKeyboardButton("Выключить", callback_data="settings:rem:off")],
                    ]
                )
                await query.message.reply_text("Напоминания:", reply_markup=keyboard)
                return

            if action == "rem":
                mode = data.split(":")[-1]
                settings.reminders_mode = mode
                db.commit()
                await query.message.reply_text(f"Напоминания: {mode} ✅", reply_markup=build_main_menu())
                return

        if data == "limit:raise":
            category = context.user_data.get("limit_alert_category")
            if not category:
                await query.message.reply_text("Категория не найдена. Открой лимиты из меню.")
                return
            state_set(context, STATE_LIMIT_SET_AMOUNT, {"category": category})
            await query.message.reply_text(f"Новый лимит для «{category}»?", reply_markup=build_cancel_menu())
            return

        if data == "limit:keep":
            context.user_data.pop("limit_alert_category", None)
            await query.message.reply_text("Оставил как есть.")
            return

        if data == "limit:move_other":
            category = context.user_data.get("limit_alert_category")
            if not category:
                await query.message.reply_text("Категория не найдена. Открой лимиты из меню.")
                return
            tx = (
                db.query(Transaction)
                .filter(
                    Transaction.user_id == user.id,
                    Transaction.kind == "expense",
                    Transaction.currency == settings.currency,
                    Transaction.category == category,
                )
                .order_by(Transaction.id.desc())
                .first()
            )
            if tx:
                tx.category = "Другое"
                db.commit()
                context.user_data.pop("limit_alert_category", None)
                await query.message.reply_text("Последний расход перенесён в «Другое» ✅")
            else:
                await query.message.reply_text("Не нашёл операцию для переноса.")
            return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла ошибка. Попробуй ещё раз.")
        except Exception:
            return


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)

    logger.info("Finance bot started")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
