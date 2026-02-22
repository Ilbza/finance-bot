# Finance Tracker Telegram Mini App

Multi-user finance tracker with Telegram Mini App auth.
Includes a separate Telegram quest bot (student/parent branching).

## Features

- Telegram login via `initData` validation
- Multi-user data isolation (`telegram_user_id` based)
- Transactions (income/expense)
- Currency support: `USD`, `EUR`, `RUB`, `CNY`
- Budgets by category and month
- Monthly summary and category chart
- CSV export
- Quest bot with branching logic, hidden scoring, reminders

## Stack

- Backend: FastAPI + SQLite + SQLAlchemy
- Frontend: React + Vite + Recharts + Telegram WebApp SDK

## Local run

### 1) Backend

```bash
cd "/Users/ilbza/Documents/New project/backend"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export APP_SECRET="change-this-secret"
export TELEGRAM_BOT_TOKEN="<your_bot_token>"
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2) Telegram Quest Bot

```bash
cd "/Users/ilbza/Documents/New project/backend"
source .venv/bin/activate
export TELEGRAM_BOT_TOKEN="<your_bot_token>"
export QUEST_REMINDER_HOURS=12
export MENTOR_CONTACT="@mentor"
# optional:
# export MENTOR_CTA_URL="https://example.com/mentor-form"
python -m app.telegram_quest_bot
```

Bot commands:

- `/start` - start or continue quest
- `/status` - current progress/state
- `/cards` - short card texts
- `/restart` - reset and start again

### 3) Telegram Finance Bot (chat сценарий)

```bash
cd "/Users/ilbza/Documents/New project/backend"
source .venv/bin/activate
export TELEGRAM_BOT_TOKEN="<your_bot_token>"
python -m app.telegram_finance_bot
```

Quick messages in chat:

- `еда 350`
- `350 еда`
- `зп 250000`
- `лимит еда 30000`
- `итоги` / `баланс` / `остаток`

### 4) Frontend

```bash
cd "/Users/ilbza/Documents/New project/frontend"
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

If backend URL differs, create `/Users/ilbza/Documents/New project/frontend/.env`:

```bash
VITE_API_URL=https://your-backend-url
```

## Deploy without domain (recommended)

- Frontend: Vercel
- Backend: Render Web Service

### Backend on Render

- New Web Service from repo
- Root directory: `backend`
- Build command: `pip install -r requirements.txt`
- Start command: `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Add env vars:
  - `APP_SECRET` = long random string
  - `TELEGRAM_BOT_TOKEN` = token from BotFather

### Frontend on Vercel

- Import repo
- Root directory: `frontend`
- Framework preset: Vite
- Env var:
  - `VITE_API_URL` = Render backend URL

## BotFather setup

1. Open `@BotFather`
2. `/mybots` -> choose your bot
3. `Bot Settings` -> `Menu Button` -> `Configure menu button`
4. URL = your Vercel frontend URL

## Security note

If bot token was shared in chat/screenshots, revoke it in BotFather and generate a new one:
- `/revoke` then `/token`
- update `TELEGRAM_BOT_TOKEN` in Render
