import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .database import Base, SessionLocal, engine
from .models import QuestState


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("quest-bot")

REMINDER_HOURS = int(os.getenv("QUEST_REMINDER_HOURS", "12"))
REMINDER_CHECK_SECONDS = int(os.getenv("QUEST_REMINDER_CHECK_SECONDS", "120"))
MENTOR_CTA_URL = os.getenv("MENTOR_CTA_URL", "").strip()
MENTOR_CONTACT = os.getenv("MENTOR_CONTACT", "@mentor").strip()

STUDENT_REMINDER_EVENTS = [
    "🪙 Мико: Сегодня новый район. Посмотрим, ты в большинстве или в элите стратега?",
    "👛 Кеш: Ты был в одном шаге от идеального решения. Хочешь увидеть лучший ход?",
    "🎯 Целья: Микро-квест дня: поймай 1 импульсивное желание и сделай паузу 10 секунд.",
]
PARENT_REMINDER_TEXT = "🪙 Мико: Квест ждёт продолжения. Следующий шаг займёт 2 минуты."

CARD_TEXTS = [
    "Деньги - это не цель. Это инструмент свободы.",
    "Доход = навык x польза x спрос",
    "Сначала накопления. Потом желания.",
    "Процент - цена за скорость.",
    "Инвестиции - это долго. Не быстро.",
    "Главный актив - ты.",
]

STUDENT_CHALLENGES = {
    1: "Сегодня попробуй отложить 5% или заметить 1 импульсивное желание.",
    2: "Запиши один навык, который можешь прокачать за 30 дней.",
    3: "Заметь 1 импульсивную покупку и сделай паузу 10 секунд.",
    4: "Выбери шаг на этой неделе, который приблизит к цели.",
    5: "Сравни: кредит сейчас vs накопление 30 дней.",
    6: "Проверь один инвестиционный совет через 2 независимых источника.",
    7: "Сохрани правило: сначала система, потом траты.",
}

PARENT_CHALLENGES = {
    1: "Обсудите с ребёнком одну покупку: это цель или импульс?",
    2: "Спросите: какой навык даст больше свободы через год?",
    3: "Определите 3-5 категорий личного бюджета ребёнка.",
    4: "Разберите одну цель по схеме: сколько, когда, как.",
    5: "Посчитайте вместе пример: цена + процент = переплата.",
    6: "Сформулируйте правило: долго, умно, регулярно.",
    7: "Выберите 1 учебную цель на 30 дней и первый шаг завтра.",
}

GOALS = {
    "gadget": "телефон/гаджет",
    "travel": "путешествие/свобода",
    "study": "учёба/поступление",
    "money": "финансовая свобода",
}


def now_utc() -> datetime:
    return datetime.utcnow()


def progress_bar(step: int, total: int) -> str:
    blocks = 8
    filled = 0 if step <= 0 else max(1, min(blocks, int(round(step / total * blocks))))
    return f"{'█' * filled}{'░' * (blocks - filled)} {step}/{total} районов пройдено"


def fmt_money(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def set_default_state(state: QuestState) -> None:
    state.lesson = 0
    state.level = 0
    state.balance = 3000.0
    state.savings = 0.0
    state.strategy_points = 0
    state.impulse_points = 0
    state.goal = ""
    state.confidence_score = None
    state.future_balance = 0.0
    state.credit_taken = False
    state.completed = False
    state.next_reminder_at = None
    state.last_reminder_sent_at = None
    state.last_action_at = now_utc()


def start_student(state: QuestState) -> None:
    set_default_state(state)
    state.user_type = "student"
    state.lesson = 1


def start_parent(state: QuestState) -> None:
    set_default_state(state)
    state.user_type = "parent"
    state.lesson = 1
    state.balance = 0.0


def get_or_create_state(db: Session, telegram_id: int, chat_id: int) -> QuestState:
    state = db.query(QuestState).filter(QuestState.telegram_id == telegram_id).first()
    if state:
        if state.chat_id != chat_id:
            state.chat_id = chat_id
        return state

    state = QuestState(telegram_id=telegram_id, chat_id=chat_id)
    set_default_state(state)
    db.add(state)
    db.flush()
    return state


def build_segment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👨‍🎓 Я школьник", callback_data="segment:student")],
            [InlineKeyboardButton("👨‍👩‍👧 Я родитель", callback_data="segment:parent")],
        ]
    )


def build_next_keyboard(button_text: str = "➡ Дальше") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data="next")]])


def build_student_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Подобрать наставника под твою цель", callback_data="cta:mentor")],
            [InlineKeyboardButton("📈 Получить план прокачки на 30 дней", callback_data="cta:plan")],
            [InlineKeyboardButton("🎮 Пройти челлендж «Неделя без импульсов»", callback_data="cta:challenge")],
        ]
    )


def build_parent_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗂 Вопросы на неделю", callback_data="pcta:questions")],
            [InlineKeyboardButton("🚀 Подобрать наставника под цель ребёнка", callback_data="pcta:mentor")],
            [InlineKeyboardButton("🔁 Начать заново", callback_data="restart")],
        ]
    )


def build_goal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📱 Телефон/гаджет", callback_data="s4:gadget")],
            [InlineKeyboardButton("✈️ Путешествие/свобода", callback_data="s4:travel")],
            [InlineKeyboardButton("🎓 Учёба/поступление", callback_data="s4:study")],
            [InlineKeyboardButton("💸 Просто больше денег", callback_data="s4:money")],
        ]
    )


def build_confidence_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for start in range(1, 11, 2):
        rows.append(
            [
                InlineKeyboardButton(str(start), callback_data=f"s7:{start}"),
                InlineKeyboardButton(str(start + 1), callback_data=f"s7:{start + 1}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_mentor_keyboard() -> Optional[InlineKeyboardMarkup]:
    if MENTOR_CTA_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Открыть подбор", url=MENTOR_CTA_URL)]])
    return None


def student_status_text(state: QuestState) -> str:
    return (
        f"💰 Баланс: {fmt_money(state.balance)}\n"
        f"🏦 Накопления: {fmt_money(state.savings)}\n"
        f"🏆 Уровень: {state.level}/7"
    )


def goal_progress_text(state: QuestState, lesson: int) -> str:
    if not state.goal:
        return ""

    base_by_lesson = {5: 58, 6: 79, 7: 100}
    base = base_by_lesson.get(lesson, 35)
    adjustment = clamp((state.strategy_points - state.impulse_points) * 2, -10, 10)
    pct = clamp(base + adjustment, 5, 100)
    return f"Ты ближе к {state.goal} на {pct}%"


def schedule_reminder(state: QuestState) -> None:
    state.next_reminder_at = now_utc() + timedelta(hours=REMINDER_HOURS)


def reminder_text_for_state(state: QuestState) -> str:
    if state.user_type != "student":
        return PARENT_REMINDER_TEXT

    lesson_index = max(1, min(7, state.lesson)) - 1
    return STUDENT_REMINDER_EVENTS[lesson_index % len(STUDENT_REMINDER_EVENTS)]


def build_student_lesson_prompt(state: QuestState) -> tuple[str, InlineKeyboardMarkup]:
    lesson = max(1, state.lesson)

    if lesson == 1:
        text = (
            f"{progress_bar(1, 7)}\n"
            "🪙 Мико: Добро пожаловать в Деньгоград. Тут деньги реально разговаривают.\n"
            "👛 Кеш: Мы не деньги ради денег. Мы инструмент свободы.\n"
            "🪙 Мико: Ты теперь герой-стратег.\n\n"
            "💰 Баланс: 3000\n"
            "📍 Район: Входные ворота\n"
            "📈 Прогресс: 1/7\n\n"
            "😎 Макс: «Беру скин и вкусняшки. Живу один раз!»\n"
            "🎯 Лера: «Сначала проверю, куда уходят деньги, и отложу часть.»\n"
            "🪙 Мико (шепчет): У Макса потом всегда «куда всё исчезло?»\n\n"
            "Что ты сделаешь с 3000?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Потрачу часть сразу (кайф)", callback_data="s1:a")],
                [InlineKeyboardButton("B) Сначала посмотрю категории расходов", callback_data="s1:b")],
                [InlineKeyboardButton("C) Отложу 10% в накопления", callback_data="s1:c")],
            ]
        )
        return text, keyboard

    if lesson == 2:
        text = (
            f"{progress_bar(2, 7)}\n"
            "📍 Район 2/7: Работополис\n"
            "🪙 Мико: Тут деньги рождаются не из «повезло», а из «я умею».\n"
            "🏦 Байт: Деньги - это благодарность за пользу.\n\n"
            "🎬 Артём: «Я научился монтировать видео. Мне платят за ролики.»\n"
            "🕹️ Дима: «Я скроллю и жду, когда что-то появится...»\n"
            "👛 Кеш: Диме платят... за что?\n"
            "🏦 Байт: Формула дохода = навык x время x спрос.\n\n"
            "Что реально увеличивает доход?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Навык + практика", callback_data="s2:a")],
                [InlineKeyboardButton("B) Надежда на удачу", callback_data="s2:b")],
                [InlineKeyboardButton("C) Делать вид, что занят", callback_data="s2:c")],
            ]
        )
        return text, keyboard

    if lesson == 3:
        warning = ""
        if state.impulse_points > 2:
            warning = (
                "\n🪙 Мико: Я заметил, ты часто действуешь быстро. "
                "Это может быть силой, а может быть ловушкой.\n"
            )

        text = (
            f"{progress_bar(3, 7)}\n"
            "📍 Район 3/7: Дыра 200 рублей\n"
            "👛 Кеш: «Я потратил чуть-чуть» - главная иллюзия бюджета.\n"
            "🪙 Мико: Чуть-чуть каждый день = «куда делись 3000?»\n"
            "☕ кофе 200 x 10\n"
            "🎮 донат 150 x 8\n"
            "🍔 перекус 300 x 6\n"
            f"{warning}"
            "Раздели 3000 по стратегии:"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) 50% нужное / 30% желания / 20% накопления", callback_data="s3:a")],
                [InlineKeyboardButton("B) 60% нужное / 20% желания / 20% накопления", callback_data="s3:b")],
                [InlineKeyboardButton("C) 70% нужное / 20% желания / 10% накопления", callback_data="s3:c")],
                [InlineKeyboardButton("D) Свой вариант", callback_data="s3:d")],
            ]
        )
        return text, keyboard

    if lesson == 4:
        text = (
            f"{progress_bar(4, 7)}\n"
            "📍 Район 4/7: Целья и магия накоплений\n"
            "🎯 Целья: Я превращаю «хочу» в «купил». Без магии, только стратегия.\n"
            "🪙 Мико: Выбирай мечту."
        )
        return text, build_goal_keyboard()

    if lesson == 5:
        goal_line = goal_progress_text(state, 5)
        prefix = f"{goal_line}\n" if goal_line else ""
        text = (
            f"{progress_bar(5, 7)}\n"
            "📍 Район 5/7: Долгозавр\n"
            "🐉 Долгозавр: Хочешь сейчас? Подпиши. Платить потом.\n"
            "🏦 Байт: Процент = цена за скорость. Кредит = переплата.\n"
            f"{prefix}"
            "Мини-игра: телефон 50 000, кредит 20% на год.\n"
            "Сколько примерно переплатишь?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) ~5 000", callback_data="s5:a")],
                [InlineKeyboardButton("B) ~10 000", callback_data="s5:b")],
                [InlineKeyboardButton("C) ~20 000", callback_data="s5:c")],
            ]
        )
        return text, keyboard

    if lesson == 6:
        goal_line = goal_progress_text(state, 6)
        prefix = f"{goal_line}\n" if goal_line else ""
        text = (
            f"{progress_bar(6, 7)}\n"
            "📍 Район 6/7: Инвестплощадь\n"
            "🏦 Байт: Деньги могут приносить деньги, если мозг включен.\n"
            "🪙 Мико: Это не казино, это стратегия.\n"
            "🐉 Долгозавр: А можно быстро удвоить?\n"
            "🏦 Байт: Быстро можно только потерять.\n"
            f"{prefix}"
            "Что нужно перед инвестициями?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Подушка/накопления", callback_data="s6:a")],
                [InlineKeyboardButton("B) Понимание риска", callback_data="s6:b")],
                [InlineKeyboardButton("C) «В тиктоке сказали»", callback_data="s6:c")],
                [InlineKeyboardButton("D) A + B", callback_data="s6:d")],
            ]
        )
        return text, keyboard

    goal_line = goal_progress_text(state, 7)
    prefix = f"{goal_line}\n" if goal_line else ""
    text = (
        f"{progress_bar(7, 7)}\n"
        "📍 Район 7/7: Главный актив\n"
        "🎯 Целья: Главный актив - не в банке. Главный актив - ты.\n"
        "🪙 Мико: Навык нельзя «сломать рынком».\n"
        "🏦 Байт: Версия А - прокачка и рост дохода. Версия Б - «как-нибудь» и ограничения.\n"
        "👛 Кеш: Разница чаще в системе, а не в таланте.\n\n"
        f"{prefix}"
        "Оцени уверенность в учёбе, которая влияет на твои цели (1-10):"
    )
    return text, build_confidence_keyboard()


def build_parent_lesson_prompt(state: QuestState) -> tuple[str, InlineKeyboardMarkup]:
    lesson = max(1, state.lesson)

    if lesson == 1:
        text = (
            f"{progress_bar(1, 7)}\n"
            "🪙 Мико: Ваш ребёнок - герой истории. Задача не морализировать, а научить думать.\n"
            "👛 Кеш: Вопрос для разговора: почему два человека с одинаковым доходом живут по-разному?\n"
            "🎯 Целья: Подсказка - дело в решениях, не в сумме.\n\n"
            "Как вам удобнее пройти ветку?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Хочу пройти как взрослый", callback_data="p1:a")],
                [InlineKeyboardButton("B) Хочу пройти вместе с ребёнком", callback_data="p1:b")],
                [InlineKeyboardButton("C) Хочу объяснять по шагам", callback_data="p1:c")],
            ]
        )
        return text, keyboard

    if lesson == 2:
        text = (
            f"{progress_bar(2, 7)}\n"
            "🏦 Байт: Важно: ребёнок должен слышать «деньги = ценность и навыки».\n"
            "🎯 Целья: Вопрос ребёнку: «Какой навык ты хочешь прокачать, чтобы через год было больше свободы?»\n\n"
            "Какой ход выберете сейчас?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Связать деньги с ценностью", callback_data="p2:a")],
                [InlineKeyboardButton("B) Выбрать навык на год", callback_data="p2:b")],
                [InlineKeyboardButton("C) Отложить разговор", callback_data="p2:c")],
            ]
        )
        return text, keyboard

    if lesson == 3:
        text = (
            f"{progress_bar(3, 7)}\n"
            "👛 Кеш: Бюджет - это не контроль, а ясность.\n"
            "Вопрос: если выделять ребёнку деньги, какие 3-5 категорий он будет вести?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Начать с 3 категорий", callback_data="p3:a")],
                [InlineKeyboardButton("B) Сразу 5 категорий", callback_data="p3:b")],
                [InlineKeyboardButton("C) Пока без рамок", callback_data="p3:c")],
            ]
        )
        return text, keyboard

    if lesson == 4:
        text = (
            f"{progress_bar(4, 7)}\n"
            "🎯 Целья: Важно не обесценивать цели ребёнка.\n"
            "Лучший вопрос: «Сколько стоит, когда хочешь и какой план?»"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Уточнить цель и срок", callback_data="p4:a")],
                [InlineKeyboardButton("B) Сказать «ерунда»", callback_data="p4:b")],
                [InlineKeyboardButton("C) Составить план вместе", callback_data="p4:c")],
            ]
        )
        return text, keyboard

    if lesson == 5:
        text = (
            f"{progress_bar(5, 7)}\n"
            "🏦 Байт: Подростку лучше говорить: «проценты = цена за скорость».\n"
            "Как объясните тему кредита?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Пугать кредитом", callback_data="p5:a")],
                [InlineKeyboardButton("B) Объяснить формулу процентов", callback_data="p5:b")],
                [InlineKeyboardButton("C) Посчитать пример вместе", callback_data="p5:c")],
            ]
        )
        return text, keyboard

    if lesson == 6:
        text = (
            f"{progress_bar(6, 7)}\n"
            "🎯 Целья: Мысль подростка часто «быстро».\n"
            "Нужно заменить на: «долго, умно, регулярно»."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("A) Обсудить риск и горизонт", callback_data="p6:a")],
                [InlineKeyboardButton("B) Обещать быстрый результат", callback_data="p6:b")],
                [InlineKeyboardButton("C) Ввести правило регулярности", callback_data="p6:c")],
            ]
        )
        return text, keyboard

    text = (
        f"{progress_bar(7, 7)}\n"
        "🎯 Целья: Вы не покупаете репетитора. Вы инвестируете в актив ребёнка.\n"
        "Что сейчас приоритетнее?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("A) Поступление", callback_data="p7:a")],
            [InlineKeyboardButton("B) Уверенность в учёбе", callback_data="p7:b")],
            [InlineKeyboardButton("C) Дисциплина", callback_data="p7:c")],
            [InlineKeyboardButton("D) Конкретный предмет", callback_data="p7:d")],
        ]
    )
    return text, keyboard


def apply_student_choice(state: QuestState, callback_data: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    if callback_data.startswith("s1:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            state.balance -= 1500
            state.impulse_points += 1
            feedback = (
                "👛 Кеш: Честно? Это нормально. Но стратег делает так: кайф + не сломать завтра.\n"
                "🪙 Мико: Без режима «всё в ноль», окей?"
            )
        elif choice == "b":
            state.strategy_points += 2
            feedback = (
                "👛 Кеш: Вот это мозг. Ты не деньги считаешь - ты привычки ловишь.\n"
                "🪙 Мико: Это уровень «я управляю жизнью, а не жизнь мной»."
            )
        else:
            state.savings += 300
            state.balance -= 300
            state.strategy_points += 3
            feedback = (
                "🎯 Целья: Ты сделал то, чего не делают большинство взрослых.\n"
                "🪙 Мико: «Начну копить с понедельника» - классика. У тебя уже старт есть."
            )

        state.level = 1
        state.lesson = 2
        schedule_reminder(state)
        text = (
            f"{feedback}\n\n"
            "👛 Кеш: Формула стратега:\n"
            "1. Сколько у меня денег?\n"
            "2. Куда они обычно уходят?\n"
            "3. Что я хочу получить в итоге?\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[1]}\n\n"
            "🪙 Мико: В следующем районе покажу, откуда реально берутся деньги."
        )
        return text, build_next_keyboard("➡ Перейти в Работополис")

    if callback_data.startswith("s2:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            state.balance += 500
            state.strategy_points += 3
            feedback = "🏦 Байт: Верно. Навык + практика = рабочий путь роста дохода."
        elif choice == "b":
            state.impulse_points += 1
            feedback = "🪙 Мико: Путь «когда-нибудь повезёт» обычно заканчивается словом «никогда»."
        else:
            state.impulse_points += 2
            feedback = "👛 Кеш: Имитация занятости не создаёт ценность. Но мы можем переключиться."

        state.level = 2
        state.lesson = 3
        schedule_reminder(state)
        text = (
            f"{feedback}\n\n"
            "🎯 Целья: Как думать:\n"
            "1. Что я уже умею?\n"
            "2. Что могу улучшить за 30 дней?\n"
            "3. Как это даст больше свободы?\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[2]}\n\n"
            "🪙 Мико: Следующий район - главная дыра «ну это же всего 200 рублей»."
        )
        return text, build_next_keyboard()

    if callback_data.startswith("s3:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            state.savings += 600
            state.balance -= 600
            state.strategy_points += 3
            feedback = "👛 Кеш: Сильный ход. Есть и жизнь сейчас, и защита будущего."
        elif choice == "b":
            state.savings += 600
            state.balance -= 600
            state.strategy_points += 2
            feedback = "👛 Кеш: Отличная дисциплина. Накопления уже создают спокойствие."
        elif choice == "c":
            state.savings += 300
            state.balance -= 300
            state.strategy_points += 1
            feedback = "🪙 Мико: Рабочий вариант. Главное - накопления включены."
        else:
            state.strategy_points += 1
            feedback = "👛 Кеш: Свой вариант норм, если правило простое и регулярное."

        state.level = 3
        state.lesson = 4
        schedule_reminder(state)
        text = (
            f"{feedback}\n\n"
            "👛 Кеш: Алгоритм стратега:\n"
            "1. Сначала накопления (хотя бы 10%)\n"
            "2. Потом нужное\n"
            "3. Потом желания\n\n"
            "🎯 Целья: Вопрос перед покупкой: «Это приближает к цели или просто делает день приятнее?»\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[3]}\n\n"
            "🪙 Мико: Дальше ты выберешь цель и путь к ней без срыва."
        )
        return text, build_next_keyboard()

    if callback_data.startswith("s4:"):
        goal_key = callback_data.split(":", 1)[1]
        state.goal = GOALS.get(goal_key, "цель")
        state.strategy_points += 1
        state.level = 4
        state.lesson = 5
        schedule_reminder(state)
        text = (
            f"🎯 Целья: Цель зафиксирована - {state.goal}.\n"
            "👛 Кеш: Алгоритм:\n"
            "1. Цель (сколько?)\n"
            "2. Срок (когда?)\n"
            "3. План (сколько откладывать?)\n"
            "4. Усилитель (как увеличить доход?)\n\n"
            "🪙 Мико: Многие здесь выбирают «как-нибудь», а потом зовут Долгозавра.\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[4]}"
        )
        return text, build_next_keyboard()

    if callback_data.startswith("s5:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            state.future_balance = -10000
            state.credit_taken = True
            state.impulse_points += 2
            feedback = (
                "🐉 Долгозавр: Люблю, когда цену скорости недооценивают.\n"
                "👛 Кеш: Это сигнал риска. В финале увидишь последствия."
            )
        elif choice == "b":
            state.strategy_points += 3
            feedback = "🏦 Байт: Верно, ближе всего к реальной переплате. Ты считаешь до решения."
        else:
            state.strategy_points += 1
            feedback = "🏦 Байт: Завышение лучше недооценки, но важно уметь считать точнее."

        state.level = 5
        state.lesson = 6
        schedule_reminder(state)
        text = (
            f"{feedback}\n\n"
            "🏦 Байт: Перед кредитом спроси:\n"
            "1. Есть план дохода?\n"
            "2. Платёж не ломает бюджет?\n"
            "3. Есть ли решение дешевле?\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[5]}\n\n"
            "🪙 Мико: Следующий район бесит Долгозавра - там деньги умеют расти."
        )
        return text, build_next_keyboard()

    if callback_data.startswith("s6:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            state.strategy_points += 1
            feedback = "🏦 Байт: Без подушки безопасность хрупкая. Верный приоритет."
        elif choice == "b":
            state.strategy_points += 1
            feedback = "🏦 Байт: Понимание риска - обязательная база."
        elif choice == "c":
            state.strategy_points -= 2
            state.impulse_points += 1
            feedback = "🪙 Мико: Тикток - не инвест-стратегия. Быстро = риск."
        else:
            state.strategy_points += 3
            feedback = "🏦 Байт: Идеально. Сначала подушка и риск, потом вход маленькой суммой."

        state.level = 6
        state.lesson = 7
        schedule_reminder(state)
        text = (
            f"{feedback}\n\n"
            "🏦 Байт: Алгоритм инвестиций:\n"
            "1. Сначала безопасность\n"
            "2. Потом понимание\n"
            "3. Потом маленький тест\n"
            "4. Потом долгий горизонт\n\n"
            f"{student_status_text(state)}\n\n"
            f"🎯 Челлендж: {STUDENT_CHALLENGES[6]}\n\n"
            "🎯 Целья: Финал рядом. Сейчас увидишь главный актив, который качается в школе."
        )
        return text, build_next_keyboard()

    if callback_data.startswith("s7:"):
        raw_score = callback_data.split(":", 1)[1]
        try:
            confidence_score = int(raw_score)
        except ValueError:
            confidence_score = 5
        confidence_score = clamp(confidence_score, 1, 10)

        state.level = 7
        state.lesson = 7
        state.completed = True
        state.next_reminder_at = None
        state.confidence_score = confidence_score

        summary_lines = [
            "🏁 Финал Деньгограда",
            f"Стратегия: {state.strategy_points}",
            f"Импульсивность: {state.impulse_points}",
            f"Баланс: {fmt_money(state.balance)}",
            f"Накопления: {fmt_money(state.savings)}",
            f"Уверенность в учёбе: {state.confidence_score}/10",
        ]

        if state.strategy_points > state.impulse_points:
            summary_lines.append("\n👛 Кеш: Ты мыслишь как стратег. Следующий шаг - прокачка навыков.")
        elif state.impulse_points > state.strategy_points:
            summary_lines.append("\n🪙 Мико: У тебя много энергии. Направь её в систему, и это станет силой.")
        else:
            summary_lines.append("\n🎯 Целья: Баланс уже есть. Закрепи его регулярными действиями.")

        if state.credit_taken:
            summary_lines.append("\n🐉 Долгозавр: Версия будущего с кредитом сложнее. Но её можно переписать.")

        summary_lines.append(
            "\n🪙 Мико: Честная оценка принята. Теперь вопрос стратега:\n"
            "Что мешает поднять уверенность в учёбе на +2 пункта за месяц?"
        )
        summary_lines.append("\n📜 Сертификат:\nФинансовый стратег. Уровень 1")
        summary_lines.append(
            "Уровень 2 - прокачка реальных навыков (математика, английский, информатика)"
        )

        text = "\n".join(summary_lines)
        return text, build_student_cta_keyboard()

    return "Не понял выбор. Нажми кнопку текущего урока.", None


def apply_parent_choice(state: QuestState, callback_data: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    if callback_data.startswith("p1:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = (
                "🪙 Мико: Отлично, идём как взрослый сценарист.\n"
                "Реплика: «Я не оцениваю, я помогаю думать»."
            )
        elif choice == "b":
            feedback = (
                "👛 Кеш: Сильный формат. Совместный разбор даёт привычку к аргументам.\n"
                "Реплика: «Давай вместе посчитаем последствия»."
            )
        else:
            feedback = (
                "🏦 Байт: Отличный выбор для системности.\n"
                "Реплика: «Шаг 1 - факт, шаг 2 - вывод, шаг 3 - следующий ход»."
            )

        state.lesson = 2
        state.level = 1
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[1]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p2:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = (
                "🏦 Байт: Верно. Деньги = ценность + польза.\n"
                "Вопрос ребёнку: «Какую проблему твой навык решает для других?»"
            )
        elif choice == "b":
            feedback = (
                "🎯 Целья: Отлично. Навык на год даёт направление и мотивацию.\n"
                "Реплика: «Что прокачаем за 30 дней?»"
            )
        else:
            feedback = (
                "🪙 Мико: Разговор лучше не откладывать. Достаточно 7 минут.\n"
                "Стартовая фраза: «Хочу понять, как ты выбираешь, без критики»."
            )

        state.lesson = 3
        state.level = 2
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[2]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p3:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = (
                "👛 Кеш: Хороший старт. 3 категории проще удержать.\n"
                "Пример: еда, транспорт, желания."
            )
        elif choice == "b":
            feedback = (
                "👛 Кеш: Для продвинутого уровня отлично.\n"
                "Добавьте: накопления, учёба, развлечения, подарки, прочее."
            )
        else:
            feedback = (
                "🪙 Мико: Без рамки ребёнку сложнее видеть картину.\n"
                "Минимум: одна таблица трат за неделю."
            )

        state.lesson = 4
        state.level = 3
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[3]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p4:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = (
                "🎯 Целья: Верно. Уточнение цели запускает мышление.\n"
                "Реплика: «Сколько стоит? Когда хочешь? Какой план?»"
            )
        elif choice == "b":
            feedback = (
                "🪙 Мико: Обесценивание убивает инициативу.\n"
                "Лучше: «Окей, цель принята. Давай посчитаем маршрут»."
            )
        else:
            feedback = (
                "👛 Кеш: Идеально. Совместный план повышает ответственность ребёнка.\n"
                "Формула: сумма/срок/ежемесячный шаг."
            )

        state.lesson = 5
        state.level = 4
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[4]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p5:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = (
                "🏦 Байт: Страх даёт короткий эффект, но не учит считать.\n"
                "Лучший фокус: математика последствий."
            )
        elif choice == "b":
            feedback = (
                "🏦 Байт: Отлично. «Процент = цена за скорость» подростки понимают быстро."
            )
        else:
            feedback = (
                "👛 Кеш: Сильный ход. Совместный расчёт снижает импульсивность.\n"
                "Реплика: «Сколько переплата в рублях?»"
            )

        state.lesson = 6
        state.level = 5
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[5]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p6:"):
        choice = callback_data.split(":", 1)[1]
        if choice == "a":
            feedback = "🎯 Целья: Отлично. Так формируется взрослая инвестиционная логика."
        elif choice == "b":
            feedback = "🐉 Долгозавр: Быстрые обещания почти всегда дороже ошибок."
        else:
            feedback = (
                "👛 Кеш: Регулярность важнее эмоций.\n"
                "Реплика: «Маленький шаг, но каждую неделю»."
            )

        state.lesson = 7
        state.level = 6
        schedule_reminder(state)
        text = f"{feedback}\n\n🎯 Челлендж: {PARENT_CHALLENGES[6]}"
        return text, build_next_keyboard()

    if callback_data.startswith("p7:"):
        choice = callback_data.split(":", 1)[1]
        priority_map = {
            "a": "поступление",
            "b": "уверенность в учёбе",
            "c": "дисциплина",
            "d": "конкретный предмет",
        }
        priority = priority_map.get(choice, "цель ребёнка")

        state.level = 7
        state.lesson = 7
        state.completed = True
        state.next_reminder_at = None
        text = (
            f"🎯 Целья: Приоритет зафиксирован - {priority}.\n"
            "Вы прошли родительскую ветку.\n"
            "Следующий шаг: подобрать наставника под цель ребёнка.\n\n"
            f"🎯 Челлендж: {PARENT_CHALLENGES[7]}"
        )
        return text, build_parent_cta_keyboard()

    return "Не понял выбор. Нажми кнопку текущего урока.", None


async def safe_remove_keyboard(update: Update) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        return


async def send_segment_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text="Кто проходит квест?",
        reply_markup=build_segment_keyboard(),
    )


async def send_student_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: QuestState) -> None:
    prompt, keyboard = build_student_lesson_prompt(state)
    await context.bot.send_message(chat_id=chat_id, text=prompt, reply_markup=keyboard)


async def send_parent_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: QuestState) -> None:
    prompt, keyboard = build_parent_lesson_prompt(state)
    await context.bot.send_message(chat_id=chat_id, text=prompt, reply_markup=keyboard)


async def send_mentor_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    text = (
        "🎯 Целья: Чтобы быстрее дойти до цели, нужен наставник-навигация:\n"
        "1. фиксируем слабые места\n"
        "2. строим понятную систему\n"
        "3. ускоряем результат без хаоса\n\n"
        f"Контакт для старта: {MENTOR_CONTACT}"
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=build_mentor_keyboard())


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    with SessionLocal() as db:
        state = get_or_create_state(db, user.id, chat.id)
        state.last_action_at = now_utc()
        db.commit()

        if not state.user_type:
            await send_segment_prompt(context, chat.id)
            return

        if state.completed:
            if state.user_type == "student":
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="Квест уже завершён. Можно открыть CTA или пройти заново.",
                    reply_markup=build_student_cta_keyboard(),
                )
            else:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="Родительская ветка уже завершена.",
                    reply_markup=build_parent_cta_keyboard(),
                )
            return

        if state.user_type == "student":
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"Продолжаем. Текущий уровень: {state.level}/7",
                reply_markup=build_next_keyboard("➡ Продолжить квест"),
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text="Продолжаем родительскую ветку.",
                reply_markup=build_next_keyboard("➡ Продолжить"),
            )


async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    with SessionLocal() as db:
        state = get_or_create_state(db, user.id, chat.id)
        set_default_state(state)
        state.user_type = ""
        db.commit()

    await context.bot.send_message(chat_id=chat.id, text="Квест сброшен.")
    await send_segment_prompt(context, chat.id)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    with SessionLocal() as db:
        state = get_or_create_state(db, user.id, chat.id)
        db.commit()

        if not state.user_type:
            await context.bot.send_message(chat_id=chat.id, text="Квест ещё не начат. Нажми /start")
            return

        if state.user_type == "student":
            text = (
                f"Сегмент: школьник\n"
                f"Текущий урок: {state.lesson}/7\n"
                f"Стратегия: {state.strategy_points}\n"
                f"Импульсивность: {state.impulse_points}\n"
                f"Цель: {state.goal or 'не выбрана'}\n"
                f"{student_status_text(state)}"
            )
        else:
            text = (
                f"Сегмент: родитель\n"
                f"Текущий урок: {state.lesson}/7\n"
                f"Завершено: {'да' if state.completed else 'нет'}"
            )

    await context.bot.send_message(chat_id=chat.id, text=text)


async def handle_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    lines = [f"{idx}. {text}" for idx, text in enumerate(CARD_TEXTS, start=1)]
    await context.bot.send_message(chat_id=chat.id, text="Карточки:\n\n" + "\n".join(lines))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not query or not user or not chat:
        return

    await query.answer()
    await safe_remove_keyboard(update)
    callback_data = query.data or ""

    with SessionLocal() as db:
        state = get_or_create_state(db, user.id, chat.id)
        state.last_action_at = now_utc()

        if callback_data == "restart":
            set_default_state(state)
            state.user_type = ""
            db.commit()
            await context.bot.send_message(chat_id=chat.id, text="Квест сброшен.")
            await send_segment_prompt(context, chat.id)
            return

        if callback_data.startswith("segment:"):
            segment = callback_data.split(":", 1)[1]
            if segment == "student":
                start_student(state)
                db.commit()
                await context.bot.send_message(chat_id=chat.id, text="Режим: школьник. Поехали.")
                await send_student_prompt(context, chat.id, state)
                return

            start_parent(state)
            db.commit()
            await context.bot.send_message(chat_id=chat.id, text="Режим: родитель. Поехали.")
            await send_parent_prompt(context, chat.id, state)
            return

        if callback_data == "next":
            if state.user_type == "student":
                if state.completed:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text="Квест уже завершён.",
                        reply_markup=build_student_cta_keyboard(),
                    )
                else:
                    db.commit()
                    await send_student_prompt(context, chat.id, state)
                return

            if state.user_type == "parent":
                if state.completed:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text="Родительская ветка завершена.",
                        reply_markup=build_parent_cta_keyboard(),
                    )
                else:
                    db.commit()
                    await send_parent_prompt(context, chat.id, state)
                return

            db.commit()
            await send_segment_prompt(context, chat.id)
            return

        if callback_data.startswith("cta:"):
            action = callback_data.split(":", 1)[1]
            if action == "mentor":
                db.commit()
                await send_mentor_message(context, chat.id)
                return

            if action == "plan":
                db.commit()
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🎯 Целья: Чтобы поднять уверенность в учёбе на +2 пункта за месяц, нужен план:\n\n"
                        "1. выявить слабые места\n"
                        "2. дать понятную систему\n"
                        "3. ускорить результат без перегруза"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("➡ Подобрать наставника", callback_data="cta:mentor")]]
                    ),
                )
                return

            if action == "challenge":
                db.commit()
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🎮 Челлендж «Неделя без импульсов»:\n"
                        "1. 7 дней фиксируй импульсивные желания\n"
                        "2. перед покупкой - пауза 10 секунд\n"
                        "3. в конце дня ответь: это приближало к цели?\n\n"
                        "🪙 Мико: Сделай 5+ успешных пауз - это уже уровень стратега."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔁 Пройти квест заново", callback_data="cta:retry")]]
                    ),
                )
                return

            if action == "retry":
                start_student(state)
                db.commit()
                await context.bot.send_message(chat_id=chat.id, text="Квест перезапущен. Новая попытка.")
                await send_student_prompt(context, chat.id, state)
                return

        if callback_data.startswith("pcta:"):
            action = callback_data.split(":", 1)[1]
            db.commit()
            if action == "questions":
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🗂 Вопросы родителя на неделю:\n"
                        "1. Эта покупка приближает тебя к цели?\n"
                        "2. Какой навык ты качаешь в этом месяце?\n"
                        "3. Если подождать 24 часа, решение изменится?\n"
                        "4. Какие 2 альтернативы дешевле?\n"
                        "5. Какой один шаг делаем до воскресенья?"
                    ),
                )
                return

            if action == "mentor":
                await send_mentor_message(context, chat.id)
                return

        if callback_data.startswith("s") and state.user_type == "student":
            text, keyboard = apply_student_choice(state, callback_data)
            db.commit()
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=keyboard)
            return

        if callback_data.startswith("p") and state.user_type == "parent":
            text, keyboard = apply_parent_choice(state, callback_data)
            db.commit()
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=keyboard)
            return

        db.commit()
        await context.bot.send_message(chat_id=chat.id, text="Не понял кнопку. Нажми /start")


async def reminder_loop(application: Application) -> None:
    while True:
        try:
            await send_due_reminders(application)
        except Exception:
            logger.exception("Failed to process reminders")
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


async def send_due_reminders(application: Application) -> None:
    due_items: list[tuple[int, int, str]] = []
    now = now_utc()

    with SessionLocal() as db:
        rows = (
            db.query(QuestState)
            .filter(
                QuestState.completed.is_(False),
                QuestState.next_reminder_at.is_not(None),
                QuestState.next_reminder_at <= now,
            )
            .all()
        )

        for state in rows:
            text = reminder_text_for_state(state)
            due_items.append((state.id, state.chat_id, text))

    sent_ids: list[int] = []
    for state_id, chat_id, text in due_items:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
            sent_ids.append(state_id)
        except Exception:
            logger.exception("Failed to send reminder to chat_id=%s", chat_id)

    if not sent_ids:
        return

    with SessionLocal() as db:
        rows = db.query(QuestState).filter(QuestState.id.in_(sent_ids)).all()
        for state in rows:
            state.next_reminder_at = None
            state.last_reminder_sent_at = now
        db.commit()


async def post_init(application: Application) -> None:
    application.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(application))
    logger.info("Reminder loop started")


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("reminder_task")
    if not task:
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def build_app(token: str) -> Application:
    return (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    Base.metadata.create_all(bind=engine)

    app = build_app(token)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("restart", handle_restart))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("cards", handle_cards))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Quest bot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
