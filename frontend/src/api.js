const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
const TOKEN_KEY = "finance_tracker_token";

let authToken = localStorage.getItem(TOKEN_KEY) || "";

function buildQuery(params) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, value);
    }
  });
  const queryString = query.toString();
  return queryString ? `?${queryString}` : "";
}

async function parseError(response) {
  const text = await response.text();
  try {
    const json = JSON.parse(text);
    return json.detail || text || "Request failed";
  } catch {
    return text || "Request failed";
  }
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };

  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`;
  }

  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers
  });

  if (!response.ok) {
    const errorMessage = await parseError(response);
    throw new Error(errorMessage);
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

export function getStoredToken() {
  return authToken;
}

export function setAuthToken(token) {
  authToken = token || "";
  if (authToken) {
    localStorage.setItem(TOKEN_KEY, authToken);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export function clearAuthToken() {
  setAuthToken("");
}

export async function loginWithTelegram(initData) {
  const result = await request("/auth/telegram", {
    method: "POST",
    body: JSON.stringify({ init_data: initData })
  });
  setAuthToken(result.access_token);
  return result;
}

export function getMe() {
  return request("/auth/me");
}

export function getTransactions(filters) {
  return request(`/transactions${buildQuery(filters)}`);
}

export function createTransaction(payload) {
  return request("/transactions", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function deleteTransaction(id) {
  return request(`/transactions/${id}`, { method: "DELETE" });
}

export function getMonthlySummary(year, month, currency) {
  return request(`/summary/month${buildQuery({ year, month, currency })}`);
}

export function getCategorySummary(year, month, kind = "expense", currency) {
  return request(`/summary/categories${buildQuery({ year, month, kind, currency })}`);
}

export function upsertBudget(payload) {
  return request("/budgets", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function getBudgets(year, month, currency) {
  return request(`/budgets${buildQuery({ year, month, currency })}`);
}

export function deleteBudget(id) {
  return request(`/budgets/${id}`, { method: "DELETE" });
}

export function getBudgetStatus(year, month, currency) {
  return request(`/budgets/status${buildQuery({ year, month, currency })}`);
}

export async function exportTransactionsCsv(filters) {
  const headers = {};
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`;
  }

  const response = await fetch(`${API_URL}/transactions/export.csv${buildQuery(filters)}`, {
    method: "GET",
    headers
  });

  if (!response.ok) {
    const errorMessage = await parseError(response);
    throw new Error(errorMessage);
  }

  const blob = await response.blob();
  const link = document.createElement("a");
  const url = URL.createObjectURL(blob);
  const fileName = `transactions-${new Date().toISOString().slice(0, 10)}.csv`;

  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
