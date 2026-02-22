import { useEffect, useMemo, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import {
  clearAuthToken,
  createTransaction,
  deleteBudget,
  deleteTransaction,
  exportTransactionsCsv,
  getBudgetStatus,
  getBudgets,
  getCategorySummary,
  getMe,
  getMonthlySummary,
  getStoredToken,
  getTransactions,
  loginWithTelegram,
  upsertBudget
} from "./api";

const CHART_COLORS = ["#1f7a8c", "#bfdbf7", "#ff7f50", "#4f772d", "#bc4749", "#3d405b"];
const CURRENCIES = ["USD", "EUR", "RUB", "CNY"];

function formatMoney(value, currency = "USD") {
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2
  }).format(Number(value || 0));
}

function currentYearMonth() {
  const now = new Date();
  return { year: now.getFullYear(), month: now.getMonth() + 1 };
}

function getDisplayName(user) {
  const full = `${user.first_name || ""} ${user.last_name || ""}`.trim();
  if (full) return full;
  if (user.username) return `@${user.username}`;
  return `id:${user.telegram_id}`;
}

export default function App() {
  const today = new Date().toISOString().slice(0, 10);
  const { year, month } = currentYearMonth();

  const [token, setToken] = useState(getStoredToken());
  const [user, setUser] = useState(null);
  const [authError, setAuthError] = useState("");
  const [telegramAttempted, setTelegramAttempted] = useState(false);

  const [activeCurrency, setActiveCurrency] = useState("USD");
  const [transactions, setTransactions] = useState([]);
  const [summary, setSummary] = useState({ income: 0, expense: 0, balance: 0 });
  const [categoryData, setCategoryData] = useState([]);
  const [budgets, setBudgets] = useState([]);
  const [budgetStatus, setBudgetStatus] = useState([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [form, setForm] = useState({
    amount: "",
    kind: "expense",
    category: "",
    txn_date: today,
    note: ""
  });

  const [budgetForm, setBudgetForm] = useState({
    category: "",
    limit_amount: ""
  });

  const [filters, setFilters] = useState({
    start_date: "",
    end_date: "",
    kind: "",
    category: ""
  });

  const totalTransactions = useMemo(() => transactions.length, [transactions]);
  const overBudgetItems = useMemo(() => budgetStatus.filter((item) => item.over_limit), [budgetStatus]);

  function resetData() {
    setTransactions([]);
    setSummary({ income: 0, expense: 0, balance: 0 });
    setCategoryData([]);
    setBudgets([]);
    setBudgetStatus([]);
  }

  function handleLogout() {
    clearAuthToken();
    setToken("");
    setUser(null);
    setTelegramAttempted(false);
    setAuthError("");
    resetData();
  }

  async function loadData(activeFilters = filters, currency = activeCurrency) {
    setLoading(true);
    setError("");

    try {
      const queryFilters = { ...activeFilters, currency };
      const [txns, monthSummary, categories, monthBudgets, monthBudgetStatus] = await Promise.all([
        getTransactions(queryFilters),
        getMonthlySummary(year, month, currency),
        getCategorySummary(year, month, "expense", currency),
        getBudgets(year, month, currency),
        getBudgetStatus(year, month, currency)
      ]);

      setTransactions(txns);
      setSummary(monthSummary);
      setCategoryData(categories);
      setBudgets(monthBudgets);
      setBudgetStatus(monthBudgetStatus);
    } catch (err) {
      setError(err.message || "Failed to load data");
    } finally {
      setLoading(false);
    }
  }

  async function tryTelegramAuth() {
    setAuthError("");

    const tg = window.Telegram?.WebApp;
    if (!tg) {
      setAuthError("Open this app inside Telegram to sign in.");
      return;
    }

    tg.ready();
    tg.expand();

    if (!tg.initData) {
      setAuthError("Telegram init data is missing. Reopen Mini App from bot menu.");
      return;
    }

    try {
      const result = await loginWithTelegram(tg.initData);
      setToken(result.access_token);
    } catch (err) {
      setAuthError(err.message || "Telegram login failed");
    }
  }

  useEffect(() => {
    if (token || telegramAttempted) return;
    setTelegramAttempted(true);
    tryTelegramAuth();
  }, [token, telegramAttempted]);

  useEffect(() => {
    if (!token) return;

    let isMounted = true;

    (async () => {
      try {
        const me = await getMe();
        if (!isMounted) return;
        setUser(me);
        await loadData(filters, activeCurrency);
      } catch (err) {
        if (!isMounted) return;
        setAuthError(err.message || "Session expired");
        handleLogout();
      }
    })();

    return () => {
      isMounted = false;
    };
  }, [token]);

  useEffect(() => {
    if (!token) return;
    loadData(filters, activeCurrency);
  }, [activeCurrency]);

  async function onSubmitTransaction(event) {
    event.preventDefault();
    setError("");

    if (!form.amount || Number(form.amount) <= 0) {
      setError("Amount must be greater than zero.");
      return;
    }
    if (!form.category.trim()) {
      setError("Category is required.");
      return;
    }

    try {
      await createTransaction({
        ...form,
        amount: Number(form.amount),
        currency: activeCurrency,
        category: form.category.trim()
      });
      setForm({ amount: "", kind: "expense", category: "", txn_date: today, note: "" });
      await loadData(filters, activeCurrency);
    } catch (err) {
      setError(err.message || "Failed to create transaction");
    }
  }

  async function onDeleteTransaction(id) {
    try {
      await deleteTransaction(id);
      await loadData(filters, activeCurrency);
    } catch (err) {
      setError(err.message || "Failed to delete transaction");
    }
  }

  async function onApplyFilters(event) {
    event.preventDefault();
    await loadData(filters, activeCurrency);
  }

  async function onExportCsv() {
    try {
      await exportTransactionsCsv({ ...filters, currency: activeCurrency });
    } catch (err) {
      setError(err.message || "Failed to export CSV");
    }
  }

  async function onSaveBudget(event) {
    event.preventDefault();
    setError("");

    if (!budgetForm.category.trim()) {
      setError("Budget category is required.");
      return;
    }
    if (!budgetForm.limit_amount || Number(budgetForm.limit_amount) <= 0) {
      setError("Budget limit must be greater than zero.");
      return;
    }

    try {
      await upsertBudget({
        year,
        month,
        currency: activeCurrency,
        category: budgetForm.category.trim(),
        limit_amount: Number(budgetForm.limit_amount)
      });
      setBudgetForm({ category: "", limit_amount: "" });
      await loadData(filters, activeCurrency);
    } catch (err) {
      setError(err.message || "Failed to save budget");
    }
  }

  async function onDeleteBudget(id) {
    try {
      await deleteBudget(id);
      await loadData(filters, activeCurrency);
    } catch (err) {
      setError(err.message || "Failed to delete budget");
    }
  }

  if (!token) {
    return (
      <main className="container">
        <section className="login-card">
          <h1>Finance Tracker</h1>
          <p>Telegram Mini App login required.</p>
          {authError ? <div className="error">{authError}</div> : null}
          <button type="button" onClick={tryTelegramAuth}>
            Sign in via Telegram
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="container">
      <header className="header header-row">
        <div>
          <h1>Finance Tracker</h1>
          <p>Track income, expenses, budgets and export CSV.</p>
        </div>
        <div className="header-actions">
          <label className="inline-field">
            Currency
            <select value={activeCurrency} onChange={(e) => setActiveCurrency(e.target.value)}>
              {CURRENCIES.map((code) => (
                <option value={code} key={code}>
                  {code}
                </option>
              ))}
            </select>
          </label>
          <span className="muted">{user ? getDisplayName(user) : "Telegram user"}</span>
          <button onClick={handleLogout}>Logout</button>
        </div>
      </header>

      {error ? <div className="error">{error}</div> : null}

      {overBudgetItems.length ? (
        <section className="warning-box">
          <strong>Budget alerts:</strong>
          <ul>
            {overBudgetItems.map((item) => (
              <li key={item.category}>
                {item.category}: exceeded by {formatMoney(Math.abs(item.remaining), activeCurrency)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="summary-grid">
        <article className="summary-card income">
          <h2>Income</h2>
          <p>{formatMoney(summary.income, activeCurrency)}</p>
        </article>
        <article className="summary-card expense">
          <h2>Expense</h2>
          <p>{formatMoney(summary.expense, activeCurrency)}</p>
        </article>
        <article className="summary-card balance">
          <h2>Balance</h2>
          <p>{formatMoney(summary.balance, activeCurrency)}</p>
        </article>
      </section>

      <section className="grid">
        <article className="panel">
          <h2>Add transaction</h2>
          <form className="form" onSubmit={onSubmitTransaction}>
            <label>
              Amount
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.amount}
                onChange={(e) => setForm((old) => ({ ...old, amount: e.target.value }))}
                required
              />
            </label>

            <label>
              Type
              <select
                value={form.kind}
                onChange={(e) => setForm((old) => ({ ...old, kind: e.target.value }))}
              >
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </label>

            <label>
              Category
              <input
                type="text"
                value={form.category}
                onChange={(e) => setForm((old) => ({ ...old, category: e.target.value }))}
                placeholder="Food, Salary, Transport..."
                required
              />
            </label>

            <label>
              Date
              <input
                type="date"
                value={form.txn_date}
                onChange={(e) => setForm((old) => ({ ...old, txn_date: e.target.value }))}
                required
              />
            </label>

            <label>
              Note
              <input
                type="text"
                value={form.note}
                onChange={(e) => setForm((old) => ({ ...old, note: e.target.value }))}
                placeholder="Optional note"
              />
            </label>

            <button type="submit">Save</button>
          </form>
        </article>

        <article className="panel">
          <h2>Expenses by category ({activeCurrency})</h2>
          <div className="chart-wrap">
            {categoryData.length ? (
              <ResponsiveContainer width="100%" height={280}>
                <PieChart>
                  <Pie data={categoryData} dataKey="total" nameKey="category" outerRadius={100}>
                    {categoryData.map((entry, index) => (
                      <Cell key={entry.category} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(value) => formatMoney(value, activeCurrency)} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <p className="muted">No expense data for this month.</p>
            )}
          </div>
        </article>
      </section>

      <section className="grid">
        <article className="panel">
          <h2>
            Category budgets ({month}/{year}, {activeCurrency})
          </h2>
          <form className="form" onSubmit={onSaveBudget}>
            <label>
              Category
              <input
                type="text"
                value={budgetForm.category}
                onChange={(e) => setBudgetForm((old) => ({ ...old, category: e.target.value }))}
                required
              />
            </label>
            <label>
              Limit
              <input
                type="number"
                min="0"
                step="0.01"
                value={budgetForm.limit_amount}
                onChange={(e) => setBudgetForm((old) => ({ ...old, limit_amount: e.target.value }))}
                required
              />
            </label>
            <button type="submit">Save budget</button>
          </form>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Limit</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {budgets.map((budget) => (
                  <tr key={budget.id}>
                    <td>{budget.category}</td>
                    <td>{formatMoney(budget.limit_amount, activeCurrency)}</td>
                    <td>
                      <button className="danger" onClick={() => onDeleteBudget(budget.id)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
                {!budgets.length ? (
                  <tr>
                    <td colSpan="3" className="muted center">
                      No budgets yet.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </article>

        <article className="panel">
          <h2>
            Budget status ({month}/{year}, {activeCurrency})
          </h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Budget</th>
                  <th>Spent</th>
                  <th>Remaining</th>
                </tr>
              </thead>
              <tbody>
                {budgetStatus.map((item) => (
                  <tr key={item.category} className={item.over_limit ? "row-warning" : ""}>
                    <td>{item.category}</td>
                    <td>{formatMoney(item.budget, activeCurrency)}</td>
                    <td>{formatMoney(item.spent, activeCurrency)}</td>
                    <td>{formatMoney(item.remaining, activeCurrency)}</td>
                  </tr>
                ))}
                {!budgetStatus.length ? (
                  <tr>
                    <td colSpan="4" className="muted center">
                      No budget activity this month.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </article>
      </section>

      <section className="panel">
        <div className="panel-header-row">
          <h2>Transactions ({totalTransactions})</h2>
          <button onClick={onExportCsv}>Export CSV</button>
        </div>

        <form className="filters" onSubmit={onApplyFilters}>
          <label>
            Start date
            <input
              type="date"
              value={filters.start_date}
              onChange={(e) => setFilters((old) => ({ ...old, start_date: e.target.value }))}
            />
          </label>
          <label>
            End date
            <input
              type="date"
              value={filters.end_date}
              onChange={(e) => setFilters((old) => ({ ...old, end_date: e.target.value }))}
            />
          </label>
          <label>
            Type
            <select
              value={filters.kind}
              onChange={(e) => setFilters((old) => ({ ...old, kind: e.target.value }))}
            >
              <option value="">All</option>
              <option value="income">Income</option>
              <option value="expense">Expense</option>
            </select>
          </label>
          <label>
            Category
            <input
              type="text"
              value={filters.category}
              onChange={(e) => setFilters((old) => ({ ...old, category: e.target.value }))}
            />
          </label>
          <button type="submit">Apply filters</button>
        </form>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Type</th>
                <th>Category</th>
                <th>Amount</th>
                <th>Note</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {transactions.map((txn) => (
                <tr key={txn.id}>
                  <td>{txn.txn_date}</td>
                  <td>{txn.kind}</td>
                  <td>{txn.category}</td>
                  <td>{formatMoney(txn.amount, txn.currency)}</td>
                  <td>{txn.note || "-"}</td>
                  <td>
                    <button className="danger" onClick={() => onDeleteTransaction(txn.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
              {!loading && transactions.length === 0 ? (
                <tr>
                  <td colSpan="6" className="muted center">
                    No transactions yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
