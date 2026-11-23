// Talks to the FastAPI backend (api.py). Set VITE_API_URL in .env if it isn't on :8000.
const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
const KEY = "opportune_token";

export const getToken = () => {
  try { return localStorage.getItem(KEY); } catch { return null; }
};
export const setToken = (t) => {
  try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch { /* storage unavailable */ }
};

let onAuthError = () => {};
export const setAuthErrorHandler = (fn) => { onAuthError = fn; };

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(path, { method = "GET", json, form } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (json !== undefined) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers,
      body: form ?? (json !== undefined ? JSON.stringify(json) : undefined),
    });
  } catch {
    throw new ApiError(`Can't reach the server at ${BASE}. Check that the API is running.`, 0);
  }

  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }

  if (!res.ok) {
    if (res.status === 401 || (res.status === 403 && data?.detail === "Not authenticated")) {
      setToken(null);
      onAuthError();
    }
    const d = data?.detail;
    const message = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => String(e.msg).replace(/^Value error, /, "")).join(" ") : "Something went wrong. Try again.";
    throw new ApiError(message, res.status);
  }
  return data;
}

// Lets the dashboard header refresh its counts.
const changed = (r) => {
  window.dispatchEvent(new Event("opps-changed"));
  return r;
};

export const api = {
  meta: () => request("/api/meta"),
  register: (email, password, extra = {}) => request("/api/auth/register", { method: "POST", json: { email, password, ...extra } }),
  login: (email, password) => request("/api/auth/login", { method: "POST", json: { email, password } }),
  getProfile: () => request("/api/profile"),
  saveProfile: (profile) => request("/api/profile", { method: "PUT", json: profile }),
  uploadCv: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("/api/profile/cv", { method: "POST", form });
  },
  search: (query, categories) => request("/api/opportunities/search", { method: "POST", json: { query, categories } }).then(changed),
  opportunities: () => request("/api/opportunities"),
  deleteAllOpportunities: () => request("/api/opportunities", { method: "DELETE" }).then(changed),
  calendar: (start, end) => request(`/api/calendar?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
  googleConnect: () => request("/api/google/connect"),
  googleDisconnect: () => request("/api/google", { method: "DELETE" }),
  checkins: () => request("/api/checkins"),
  checkin: (id, done, note) => request(`/api/blocks/${id}/checkin`, { method: "POST", json: { done, note } }).then(changed),
  agentStatus: () => request("/api/agent/status"),
  agentPulse: () => request("/api/agent/pulse"),
  agentLog: () => request("/api/agent/log"),
  agentQuestions: () => request("/api/agent/questions"),
  answerQuestion: (id, answer = "") => request(`/api/agent/log/${id}/answer`, { method: "POST", json: { answer } }).then(changed),
  skipOpportunity: (id) => request(`/api/opportunities/${id}/skip`, { method: "POST" }).then(changed),
  agentThinking: () => request("/api/agent/thinking"),
  runAgent: () => request("/api/agent/run", { method: "POST" }).then(changed),
  applications: () => request("/api/applications"),
  changeStage: (id, form) => request(`/api/applications/${id}/stage`, { method: "POST", form }).then(changed),
  approve: (ids) => request("/api/opportunities/approve", { method: "POST", json: ids }).then(changed),
};
