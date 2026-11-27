let csrf = "";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init.method && init.method !== "GET") headers["X-CSRF-Token"] = csrf;
  const res = await fetch(path, { credentials: "same-origin", ...init, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body.error ?? res.statusText);
  return body as T;
}

export const api = {
  async login(password: string) {
    const r = await call<{ csrf: string }>("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    csrf = r.csrf;
  },
  async restore() {
    const r = await call<{ csrf: string }>("/api/session");
    csrf = r.csrf;
  },
  logout: () => call("/api/logout", { method: "POST" }),
  status: () => call<Status>("/api/status"),
  signals: () => call<SignalRow[]>("/api/signals?limit=30"),
  performance: () => call<Performance>("/api/performance"),
  activity: () => call<ActivityRow[]>("/api/activity?limit=100"),
  control: (name: string, confirm?: string) =>
    call(`/api/controls/${name}`, { method: "POST", body: JSON.stringify(confirm ? { confirm } : {}) }),
};

export type Alert = { ts: string; event: string; severity: string; message: string };
export type PositionRow = {
  position_id: number; symbol: string; type: string; strike: string; expiry: string; entry: string;
  current: string | null; quantity: number; unrealised: string | null; stop: string; target: string;
  minutes_in_trade: number;
};
export type Status = {
  state: string; mode: string; live_enabled: boolean; last_error: string | null; now?: string;
  paused?: boolean; emergency_lock?: boolean; day_lock?: string | null; week_lock?: string | null;
  paused_until?: string | null; data_problems?: string[]; market_open?: boolean; trading_day?: boolean | null;
  analysis_instrument?: string | null; spot?: number | null; vix?: number | null;
  analysis?: {
    ts: string; direction: string; score_ex_liquidity: number; components: Record<string, number>;
    weights: Record<string, number>; vetoes: string[]; notes: string[]; features: Record<string, number | null>;
    invalidation: number | null;
  } | null;
  selection?: {
    contract: string | null; expiry: string | null; strike: string | null; bid: string | null; ask: string | null;
    lot_size: number | null; delta: number | null; liquidity_points: number; reasons: string[];
  } | null;
  positions?: PositionRow[];
  risk?: Record<string, string | number | null>;
  alerts?: Alert[];
};
export type SignalRow = { id: number; ts: string; direction: string; score: number; status: string; reason: string };
export type Performance = {
  metrics: Record<string, number | null>; daily_pnl: { date: string; pnl: number }[]; disclaimer: string;
};
export type ActivityRow = { ts: string; kind: string; message: string };
