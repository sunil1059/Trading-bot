"use client";
import { useCallback, useEffect, useState } from "react";
import Controls from "@/components/Controls";
import PnlChart from "@/components/PnlChart";
import { inr, num, Panel, Stat, time, tone } from "@/components/ui";
import { ActivityRow, api, ApiError, Performance, SignalRow, Status } from "@/lib/api";

function Login({ onLogin }: { onLogin: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  return (
    <main className="flex min-h-screen items-center justify-center">
      <form
        className="w-80 space-y-3 rounded-lg border border-slate-800 bg-slate-900 p-6"
        onSubmit={async (e) => {
          e.preventDefault();
          try {
            await api.login(pw);
            onLogin();
          } catch (x) {
            setErr((x as Error).message);
          }
        }}
      >
        <h1 className="text-lg font-semibold">Options Bot</h1>
        <input type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} placeholder="Dashboard password"
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2" />
        <button className="w-full rounded bg-sky-600 py-2 font-medium text-white">Log in</button>
        {err && <p className="text-sm text-rose-400">{err}</p>}
      </form>
    </main>
  );
}

function ModeBanner({ s }: { s: Status }) {
  const live = s.live_enabled;
  const locked = s.emergency_lock || s.day_lock || s.week_lock;
  return (
    <div className={`sticky top-0 z-10 flex flex-wrap items-center gap-3 px-4 py-2 text-sm font-semibold ${live ? "bg-rose-700" : "bg-sky-800"}`}>
      <span className="rounded bg-black/30 px-2 py-0.5">MODE: {s.mode.toUpperCase()}</span>
      <span>{live ? "LIVE ORDERS" : "No real orders are placed"}</span>
      <span className="font-normal">Engine: {s.state}</span>
      {s.paused && <span className="rounded bg-amber-500/30 px-2">PAUSED</span>}
      {locked && <span className="rounded bg-rose-500/40 px-2">LOCKED: {s.emergency_lock ? "emergency" : s.day_lock || s.week_lock}</span>}
      {s.now && <span className="ml-auto font-normal">{time(s.now)} IST</span>}
    </div>
  );
}

export default function Page() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [s, setS] = useState<Status | null>(null);
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [perf, setPerf] = useState<Performance | null>(null);
  const [activity, setActivity] = useState<ActivityRow[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [st, sg, pf, ac] = await Promise.all([api.status(), api.signals(), api.performance(), api.activity()]);
      setS(st); setSignals(sg); setPerf(pf); setActivity(ac);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setAuthed(false);
    }
  }, []);

  useEffect(() => {
    api.restore().then(() => setAuthed(true)).catch(() => setAuthed(false));
  }, []);
  useEffect(() => {
    if (!authed) return;
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [authed, refresh]);

  if (authed === null) return null;
  if (!authed) return <Login onLogin={() => setAuthed(true)} />;
  if (!s) return <p className="p-6 text-slate-400">Loading…</p>;

  const a = s.analysis;
  const sel = s.selection;
  const r = s.risk ?? {};
  const m = perf?.metrics ?? {};

  return (
    <div>
      <ModeBanner s={s} />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Controls paused={s.paused} emergency={s.emergency_lock} onDone={refresh} />
          {s.state !== "running" && (
            <a href="/auth/kite/login" className="rounded bg-emerald-700 px-3 py-2 text-sm font-medium text-white">
              Log in to Zerodha to start
            </a>
          )}
        </div>
        {s.last_error && <p className="rounded border border-rose-800 bg-rose-950 p-2 text-sm text-rose-300">{s.last_error}</p>}
        {!!s.data_problems?.length && (
          <p className="rounded border border-amber-800 bg-amber-950 p-2 text-sm text-amber-200">
            New entries blocked: {s.data_problems.join("; ")}
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-3">
          <Panel title="A · Live market">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="NIFTY spot" value={num(s.spot ?? null)} />
              <Stat label="India VIX" value={num(s.vix ?? null)} />
              <Stat label="Analysis series" value={<span className="text-sm">{s.analysis_instrument ?? "—"}</span>} />
              <Stat label="Market" value={s.market_open ? "Open" : "Closed"} />
              <Stat label="VWAP" value={num(a?.features.vwap)} />
              <Stat label="RSI 14 (5m)" value={num(a?.features.rsi, 1)} />
              <Stat label="EMA 9 / 21" value={<span className="text-sm">{num(a?.features.ema9)} / {num(a?.features.ema21)}</span>} />
              <Stat label="EMA 50" value={num(a?.features.ema50)} />
              <Stat label="Rel. volume" value={num(a?.features.rvol)} />
              <Stat label="Opening range" value={<span className="text-sm">{num(a?.features.orl)} – {num(a?.features.orh)}</span>} />
            </div>
          </Panel>

          <Panel title="B · Signal" right={a && <span className="text-xs text-slate-500">{time(a.ts)}</span>}>
            {!a ? <p className="text-sm text-slate-500">Waiting for the first completed 5-minute bar.</p> : (
              <div className="space-y-2">
                <div className={`text-2xl font-bold ${a.direction === "CE" ? "text-emerald-400" : a.direction === "PE" ? "text-rose-400" : "text-slate-400"}`}>
                  {a.direction === "NO_TRADE" ? "NO TRADE" : `BUY ${a.direction}`}
                </div>
                <div className="text-sm">Score <span className="num font-semibold">{a.score_ex_liquidity + (sel?.liquidity_points ?? 0)}</span>/100
                  <span className="ml-1 text-xs text-slate-500">(setup quality, not a win probability)</span></div>
                <ul className="grid grid-cols-2 gap-x-4 text-xs text-slate-400">
                  {Object.entries(a.weights).map(([k, w]) => (
                    <li key={k} className="flex justify-between"><span>{k}</span>
                      <span className="num">{k === "liquidity" ? sel?.liquidity_points ?? 0 : a.components[k] ?? 0}/{w}</span></li>
                  ))}
                </ul>
                {sel?.contract && (
                  <div className="grid grid-cols-2 gap-2 border-t border-slate-800 pt-2 text-sm">
                    <span>Contract</span><span className="num">{sel.contract}</span>
                    <span>Strike / expiry</span><span className="num">{sel.strike} / {sel.expiry}</span>
                    <span>Bid / ask</span><span className="num">{sel.bid} / {sel.ask}</span>
                    <span>Lot size</span><span className="num">{sel.lot_size}</span>
                    <span>Delta</span><span className="num">{num(sel.delta)}</span>
                    <span>Invalidation</span><span className="num">{num(a.invalidation)}</span>
                  </div>
                )}
                {[...a.vetoes, ...a.notes, ...(sel && !sel.contract ? sel.reasons : [])].map((v) => (
                  <p key={v} className="text-xs text-amber-300">• {v}</p>
                ))}
              </div>
            )}
          </Panel>

          <Panel title="E · Risk control">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Capital" value={inr(r.capital as number, 0)} />
              <Stat label="Available" value={inr(r.available_funds as string, 0)} />
              <Stat label="Deployed" value={inr(r.deployed as string, 0)} />
              <Stat label="Max daily loss" value={inr(r.max_daily_loss as number, 0)} />
              <Stat label="Remaining daily risk" value={inr(r.remaining_daily_risk as string, 0)} tone="warn" />
              <Stat label="Trades left today" value={r.trades_remaining as number} />
              <Stat label="Realised today" value={inr(r.realised_today as string)} tone={tone(r.realised_today)} />
              <Stat label="Open MTM" value={inr(r.open_mtm as string)} tone={tone(r.open_mtm)} />
              <Stat label="Consecutive losses" value={r.consecutive_losses as number} />
              <Stat label="Max loss / trade" value={inr(r.max_loss_per_trade as number, 0)} />
            </div>
          </Panel>
        </div>

        <Panel title="C · Open positions">
          {!s.positions?.length ? <p className="text-sm text-slate-500">No open positions.</p> : (
            <table className="num w-full text-sm">
              <thead className="text-left text-xs text-slate-500"><tr>
                {["Instrument", "Type", "Strike", "Expiry", "Entry", "Current", "Qty", "Unrealised", "Stop", "Target", "Min"].map((h) => <th key={h} className="py-1">{h}</th>)}
              </tr></thead>
              <tbody>{s.positions.map((p) => (
                <tr key={p.position_id} className="border-t border-slate-800">
                  <td className="py-1">{p.symbol}</td><td>{p.type}</td><td>{p.strike}</td><td>{p.expiry}</td><td>{p.entry}</td>
                  <td>{p.current ?? "—"}</td><td>{p.quantity}</td>
                  <td className={tone(p.unrealised) === "bad" ? "text-rose-400" : "text-emerald-400"}>{inr(p.unrealised)}</td>
                  <td>{p.stop}</td><td>{p.target}</td><td>{p.minutes_in_trade}</td>
                </tr>))}</tbody>
            </table>
          )}
        </Panel>

        <div className="grid gap-4 lg:grid-cols-2">
          <Panel title="D · Performance" right={<span className="text-xs text-slate-500">{perf?.disclaimer}</span>}>
            <div className="mb-3 grid grid-cols-3 gap-3 md:grid-cols-4">
              <Stat label="Trades" value={m.total_trades} />
              <Stat label="Won / lost" value={`${m.winning_trades ?? 0} / ${m.losing_trades ?? 0}`} />
              <Stat label="Win rate" value={m.win_rate == null ? "—" : `${(m.win_rate * 100).toFixed(0)}%`} />
              <Stat label="Total P&L" value={inr(m.total_pnl)} tone={tone(m.total_pnl)} />
              <Stat label="Avg win" value={inr(m.avg_win)} />
              <Stat label="Avg loss" value={inr(m.avg_loss)} />
              <Stat label="Profit factor" value={num(m.profit_factor)} />
              <Stat label="Max drawdown" value={inr(m.max_drawdown)} />
            </div>
            <PnlChart data={perf?.daily_pnl ?? []} />
          </Panel>

          <Panel title="Recent signals">
            <div className="max-h-72 overflow-auto">
              <table className="num w-full text-xs">
                <tbody>{signals.map((g) => (
                  <tr key={g.id} className="border-t border-slate-800 align-top">
                    <td className="py-1 pr-2 text-slate-500">{time(g.ts)}</td>
                    <td className="pr-2">{g.direction}</td><td className="pr-2">{g.score}</td>
                    <td className={`pr-2 ${g.status === "ACCEPTED" ? "text-emerald-400" : "text-slate-400"}`}>{g.status}</td>
                    <td className="text-slate-400">{g.reason}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          </Panel>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Panel title="F · Activity log">
            <div className="max-h-80 overflow-auto text-xs">
              {activity.map((x, i) => (
                <div key={i} className="flex gap-2 border-t border-slate-800 py-1">
                  <span className="num shrink-0 text-slate-500">{time(x.ts)}</span>
                  <span className="shrink-0 font-medium text-slate-300">{x.kind}</span>
                  <span className="text-slate-400">{x.message}</span>
                </div>))}
            </div>
          </Panel>
          <Panel title="Alerts">
            <div className="max-h-80 overflow-auto text-xs">
              {(s.alerts ?? []).map((x, i) => (
                <div key={i} className="flex gap-2 border-t border-slate-800 py-1">
                  <span className="num shrink-0 text-slate-500">{time(x.ts)}</span>
                  <span className={`shrink-0 font-medium ${x.severity === "critical" ? "text-rose-400" : x.severity === "warning" ? "text-amber-300" : "text-slate-300"}`}>{x.event}</span>
                  <span className="text-slate-400">{x.message}</span>
                </div>))}
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}
