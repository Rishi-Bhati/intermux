"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import styles from "./HistoryChart.module.css";

type Props = {
  /** Event times, oldest first. The line counts them: 1, 2, 3 and so on. */
  times: number[];
  /** What each event is, for the tooltip, e.g. "commit". */
  noun: string;
  releases: Array<{ tag: string; t: number }>;
  end: number;
};

const HEIGHT = 236;
const PAD = { left: 34, right: 14, top: 30, bottom: 28 };

const MONTH = new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit", timeZone: "UTC" });
const FULL = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

const tagName = (tag: string) => tag.replace(/^linux-/, "").replace(/^v/, "v");

/** Four even steps that reach at least `n`, using round numbers (5, 10, 20, 25, 50 and so on). */
function niceScale(n: number) {
  const raw = Math.max(n, 4) / 4;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = ([1, 2, 2.5, 5, 10].find((m) => raw <= m * pow) ?? 10) * pow;
  return { step, top: step * 4 };
}

/** A growth curve with the releases flagged on it. Hover to read any day. */
export default function HistoryChart({ times, noun, releases, end }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(280, Math.round(el.getBoundingClientRect().width)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const geometry = useMemo(() => {
    const t0 = times[0] ?? end - 1;
    const t1 = Math.max(end, times[times.length - 1] ?? end);
    const total = times.length;
    const { step, top } = niceScale(total);
    const innerW = width - PAD.left - PAD.right;
    const innerH = HEIGHT - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + ((t - t0) / (t1 - t0)) * innerW;
    const y = (v: number) => PAD.top + innerH - (v / top) * innerH;

    // A staircase: the count holds until the next event.
    let line = `M${x(t0).toFixed(1)} ${y(0).toFixed(1)}`;
    times.forEach((t, i) => {
      line += ` L${x(t).toFixed(1)} ${y(i).toFixed(1)} L${x(t).toFixed(1)} ${y(i + 1).toFixed(1)}`;
    });
    line += ` L${x(t1).toFixed(1)} ${y(total).toFixed(1)}`;
    const area = `${line} L${x(t1).toFixed(1)} ${y(0).toFixed(1)} L${x(t0).toFixed(1)} ${y(0).toFixed(1)} Z`;

    // Quarter-year ticks along the bottom.
    const ticks: Array<{ t: number; label: string }> = [];
    const start = new Date(t0);
    let cursor = Date.UTC(start.getUTCFullYear(), Math.ceil(start.getUTCMonth() / 3) * 3, 1);
    const spanMonths = (t1 - t0) / (30.4 * 86400000);
    const every = spanMonths > 14 ? 6 : 3;
    let n = 0;
    while (cursor < t1 && n < 40) {
      if (cursor >= t0) ticks.push({ t: cursor, label: MONTH.format(cursor) });
      const d = new Date(cursor);
      cursor = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + every, 1);
      n++;
    }

    const grid = [0, 1, 2, 3, 4].map((i) => ({ v: step * i, y: y(step * i) }));

    // Labels for releases, skipping any that would crowd the one before.
    const lastOfDay = releases.filter((r, i) => {
      const next = releases[i + 1];
      return !next || Math.floor(next.t / 86400000) !== Math.floor(r.t / 86400000);
    });
    const inRange = lastOfDay.filter((r) => r.t >= t0 && r.t <= t1).map((r) => ({ ...r, x: x(r.t) }));
    const flags = inRange.map((r, i) => {
      const labelled = inRange.slice(0, i).filter((p, j) => j === 0 || r.x - p.x > 100);
      const crowded = inRange.slice(0, i).some((p) => r.x - p.x <= 100);
      return { ...r, showLabel: !crowded || labelled.length === 0, flip: r.x > width - 80 };
    });

    return { t0, t1, total, top, innerW, x, y, line, area, ticks, grid, flags };
  }, [times, releases, end, width]);

  const countAt = (t: number) => {
    let lo = 0;
    let hi = times.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] <= t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  };

  const onMove = (e: React.PointerEvent<SVGRectElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setHover(e.clientX - rect.left + PAD.left);
  };

  const hoverT =
    hover === null
      ? null
      : geometry.t0 + ((hover - PAD.left) / geometry.innerW) * (geometry.t1 - geometry.t0);
  const hoverCount = hoverT === null ? 0 : countAt(hoverT);
  const nearFlag =
    hover === null ? null : geometry.flags.find((f) => Math.abs(f.x - hover) < 7) ?? null;

  return (
    <div ref={wrap} className={styles.wrap}>
      <svg width={width} height={HEIGHT} className={styles.svg} role="img" aria-label={`Growth in ${noun}s over time`}>
        <defs>
          <linearGradient id="history-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#3a5aff" stopOpacity="0.26" />
            <stop offset="1" stopColor="#3a5aff" stopOpacity="0" />
          </linearGradient>
        </defs>

        {geometry.grid.map((g) => (
          <g key={g.v}>
            <line x1={PAD.left} x2={width - PAD.right} y1={g.y} y2={g.y} className={styles.grid} />
            <text x={PAD.left - 8} y={g.y} className={styles.yLabel}>
              {g.v}
            </text>
          </g>
        ))}

        {geometry.ticks.map((t) => (
          <text key={t.t} x={geometry.x(t.t)} y={HEIGHT - 8} className={styles.xLabel}>
            {t.label}
          </text>
        ))}

        {geometry.flags.map((f) => (
          <g key={f.tag + f.t} className={styles.flag} data-near={nearFlag === f || undefined}>
            <line x1={f.x} x2={f.x} y1={PAD.top - 6} y2={HEIGHT - PAD.bottom} />
            <rect x={f.x - 2.5} y={PAD.top - 11} width="5" height="5" rx="1" />
            {f.showLabel && (
              <text x={f.flip ? f.x - 8 : f.x + 8} y={PAD.top - 5} textAnchor={f.flip ? "end" : "start"}>
                {tagName(f.tag)}
              </text>
            )}
          </g>
        ))}

        <path d={geometry.area} fill="url(#history-fill)" />
        <path d={geometry.line} className={styles.line} />

        {hover !== null && hoverT !== null && (
          <g className={styles.cursor}>
            <line x1={hover} x2={hover} y1={PAD.top - 6} y2={HEIGHT - PAD.bottom} />
            <circle cx={hover} cy={geometry.y(hoverCount)} r="4" />
          </g>
        )}

        <rect
          x={PAD.left}
          y={0}
          width={geometry.innerW}
          height={HEIGHT}
          fill="transparent"
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        />
      </svg>

      {hover !== null && hoverT !== null && (
        <div
          className={styles.tip}
          style={{ left: Math.min(Math.max(hover, 70), width - 70), top: Math.max(geometry.y(hoverCount) - 14, 4) }}
        >
          <strong>
            {hoverCount} {noun}
            {hoverCount === 1 ? "" : "s"}
          </strong>
          <span>{FULL.format(hoverT)}</span>
          {nearFlag && <em>Release {tagName(nearFlag.tag)}</em>}
        </div>
      )}
    </div>
  );
}
