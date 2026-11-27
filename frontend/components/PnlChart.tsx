"use client";
import { useEffect, useRef } from "react";
import { createChart, HistogramSeries, type IChartApi } from "lightweight-charts";

export default function PnlChart({ data }: { data: { date: string; pnl: number }[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  useEffect(() => {
    if (!ref.current) return;
    const c = createChart(ref.current, {
      height: 160, layout: { background: { color: "transparent" }, textColor: "#94a3b8" },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1e293b" } }, rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
    });
    chart.current = c;
    const s = c.addSeries(HistogramSeries, {});
    s.setData(data.map((d) => ({ time: d.date, value: d.pnl, color: d.pnl >= 0 ? "#34d399" : "#fb7185" })));
    c.timeScale().fitContent();
    return () => c.remove();
  }, [data]);
  if (!data.length) return <p className="text-sm text-slate-500">No closed trades yet.</p>;
  return <div ref={ref} />;
}
