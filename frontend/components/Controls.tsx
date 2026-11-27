"use client";
import { useState } from "react";
import { api } from "@/lib/api";

export default function Controls({ paused, emergency, onDone }: { paused?: boolean; emergency?: boolean; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  const run = async (name: string, phrase?: string) => {
    let confirm: string | undefined;
    if (phrase) {
      confirm = window.prompt(`Type ${phrase} to confirm`) ?? "";
      if (confirm !== phrase) return setMsg("Cancelled");
    }
    try {
      await api.control(name, confirm);
      setMsg(`${name}: done`);
    } catch (e) {
      setMsg(`${name}: ${(e as Error).message}`);
    }
    onDone();
  };
  const btn = "rounded px-3 py-2 text-sm font-medium border border-slate-700 hover:bg-slate-800";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button className="rounded bg-rose-600 px-4 py-2 text-sm font-bold text-white hover:bg-rose-500" onClick={() => run("emergency-stop")}>
        EMERGENCY STOP
      </button>
      {paused ? (
        <button className={btn} onClick={() => run("resume", "RESUME")}>Resume</button>
      ) : (
        <button className={btn} onClick={() => run("pause")}>Pause</button>
      )}
      <button className={btn} onClick={() => run("close-all", "CLOSE ALL")}>Close all positions</button>
      <button className={btn} onClick={() => run("cancel-all")}>Cancel pending orders</button>
      <button className={btn} onClick={() => run("disable-live")}>Disable live</button>
      {emergency && <button className={btn} onClick={() => run("reset-emergency", "RESET")}>Reset emergency lock</button>}
      {msg && <span className="text-xs text-slate-400">{msg}</span>}
    </div>
  );
}
