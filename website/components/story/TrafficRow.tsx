"use client";

import { useEffect, useRef, type ReactNode } from "react";
import usePrefersReducedMotion from "@/hooks/usePrefersReducedMotion";
import { CheckIcon, IdleIcon, WarnIcon } from "./icons";
import styles from "./Monitor.module.css";

export type Tone = "ok" | "hot" | "idle";

type Props = {
  /** Short name shown under the icon, e.g. "Ethernet". */
  label: string;
  icon: ReactNode;
  /** Traffic in MB/s (0 to max). */
  mbps: number;
  /** Top of the graph in MB/s: roughly what this connection can carry. */
  max: number;
  tone: Tone;
  chips: ReactNode;
};

const SPACING = 3; // px between samples
const SPEED = 38; // px per second the graph scrolls

const COLORS: Record<Tone, [number, number, number]> = {
  ok: [63, 214, 154],
  hot: [255, 133, 133],
  idle: [134, 139, 145],
};

const mix = (a: number, b: number, t: number) => a + (b - a) * t;

export default function TrafficRow({ label, icon, mbps, max, tone, chips }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const statRef = useRef<HTMLSpanElement>(null);
  const reduced = usePrefersReducedMotion();
  const props = useRef({ mbps, max, tone });

  useEffect(() => {
    props.current = { mbps, max, tone };
  }, [mbps, max, tone]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const stat = statRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !stat || !ctx) return;

    let w = 0;
    let h = 0;
    let dpr = 1;
    let raf = 0;
    let running = false;
    let visible = true;
    let last = 0;
    let acc = 0;
    let shown = "";

    const start = props.current;
    const s = {
      level: start.mbps / start.max,
      sat: start.tone === "hot" ? 1 : 0,
      color: [...COLORS[start.tone]] as [number, number, number],
      value: 0,
      samples: [] as number[],
      /** The colour each sample was drawn in, so old traffic keeps the colour it had at the time. */
      tints: [] as string[],
    };

    const sample = (t: number) => {
      const p = props.current;
      const base = s.level;
      let raw: number;
      if (base < 0.012) raw = 0;
      // Saturated: pinned to the limit line, so the number reads the same as the label above it.
      else if (p.tone === "hot") raw = base * (0.99 + Math.random() * 0.01);
      else raw = base * (1 + (Math.random() - 0.5) * 0.2 + Math.sin(t * 0.0013) * 0.05);
      s.value += (raw - s.value) * 0.55;
      return Math.min(Math.max(s.value, 0), 1);
    };

    const tint = () => s.color.map(Math.round).join(",");

    const fill = () => {
      const n = Math.ceil(w / SPACING) + 3;
      s.samples = Array.from({ length: n }, () => sample(performance.now()));
      s.tints = Array.from({ length: n }, () => tint());
    };

    const draw = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const n = s.samples.length;
      const top = 4;
      const bottom = h - 2;
      const yOf = (v: number) => bottom - v * (bottom - top);
      const xOf = (i: number) => w - (n - 1 - i) * SPACING - acc;
      const [r, g, b] = s.color.map(Math.round);
      const color = `${r},${g},${b}`;

      // The area under the line goes first, so the grid and the line sit on top of it. Its colour follows
      // the line from left to right, then a vertical fade is cut out of it so it dissolves toward the bottom.
      if (n > 1) {
        ctx.beginPath();
        s.samples.forEach((v, i) => {
          const x = xOf(i);
          if (i === 0) ctx.moveTo(x, yOf(v));
          else ctx.lineTo(x, yOf(v));
        });
        ctx.lineTo(xOf(n - 1), bottom);
        ctx.lineTo(xOf(0), bottom);
        ctx.closePath();
        const x0 = xOf(0);
        const x1 = xOf(n - 1);
        const across = ctx.createLinearGradient(x0, 0, x1, 0);
        for (let i = 0; i < n; i += 4) across.addColorStop(i / (n - 1), `rgba(${s.tints[i]},0.3)`);
        across.addColorStop(1, `rgba(${s.tints[n - 1]},0.3)`);
        ctx.fillStyle = across;
        ctx.fill();
        ctx.globalCompositeOperation = "destination-out";
        const fade = ctx.createLinearGradient(0, top, 0, bottom);
        fade.addColorStop(0, "rgba(0,0,0,0)");
        fade.addColorStop(1, "rgba(0,0,0,0.93)");
        ctx.fillStyle = fade;
        ctx.fillRect(0, 0, w, h);
        ctx.globalCompositeOperation = "source-over";
      }

      // Gridlines at 25, 50 and 75 percent, so the height of the line can be read.
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(255,255,255,0.055)";
      for (const f of [0.25, 0.5, 0.75]) {
        const y = Math.round(yOf(f)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      ctx.strokeStyle = "rgba(255,255,255,0.09)";
      ctx.beginPath();
      ctx.moveTo(0, bottom + 0.5);
      ctx.lineTo(w, bottom + 0.5);
      ctx.stroke();

      // The limit of the connection. It turns red when traffic reaches it.
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = s.sat > 0.02 ? `rgba(255,133,133,${0.25 + 0.4 * s.sat})` : "rgba(255,255,255,0.2)";
      ctx.beginPath();
      ctx.moveTo(0, top + 0.5);
      ctx.lineTo(w, top + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);

      // Traffic over the last minute, each segment in the colour it had when it was drawn.
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.lineWidth = 1.75;
      for (let i = 1; i < n; i++) {
        const v = s.samples[i];
        ctx.strokeStyle = `rgba(${s.tints[i]},${v < 0.012 ? 0.4 : 1})`;
        ctx.beginPath();
        ctx.moveTo(xOf(i - 1), yOf(s.samples[i - 1]));
        ctx.lineTo(xOf(i), yOf(v));
        ctx.stroke();
      }

      // "Now": the newest sample.
      const nowX = Math.min(xOf(n - 1), w - 4);
      const nowY = yOf(s.samples[n - 1]);
      ctx.fillStyle = "#121315";
      ctx.beginPath();
      ctx.arc(nowX, nowY, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = `rgb(${color})`;
      ctx.beginPath();
      ctx.arc(nowX, nowY, 3.2, 0, Math.PI * 2);
      ctx.fill();

    };

    // The number follows the newest point on the line, a few times a second, so it reads like a live meter.
    let statClock = 0;
    const updateStat = () => {
      const latest = s.samples.length ? s.samples[s.samples.length - 1] : s.level;
      const text = `${Math.round((reduced ? s.level : latest) * props.current.max)}`;
      if (text !== shown) {
        shown = text;
        stat.textContent = text;
      }
    };

    const ease = (rate: number, dt: number) => 1 - Math.exp(-dt * rate);

    const frame = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.1);
      last = now;
      const p = props.current;
      s.level += (p.mbps / p.max - s.level) * ease(2.4, dt);
      s.sat += ((p.tone === "hot" ? 1 : 0) - s.sat) * ease(4, dt);
      const target = COLORS[p.tone];
      s.color = s.color.map((c, i) => mix(c, target[i], ease(3, dt))) as [number, number, number];
      acc += SPEED * dt;
      while (acc >= SPACING) {
        acc -= SPACING;
        s.samples.push(sample(now));
        s.samples.shift();
        s.tints.push(tint());
        s.tints.shift();
      }
      draw();
      statClock += dt;
      if (statClock > 0.22) {
        statClock = 0;
        updateStat();
      }
      if (running) raf = requestAnimationFrame(frame);
    };

    const play = () => {
      if (running || reduced || !visible) return;
      running = true;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(raf);
    };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width;
      h = rect.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      fill();
      draw();
      updateStat();
    };

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    const io = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) play();
      else stop();
    });
    io.observe(canvas);
    play();

    // Reduced motion: no loop, so redraw once whenever the targets change.
    let poll = 0;
    if (reduced) {
      let key = "";
      poll = window.setInterval(() => {
        const p = props.current;
        const next = `${p.mbps}${p.max}${p.tone}`;
        if (next === key) return;
        key = next;
        s.level = p.mbps / p.max;
        s.sat = p.tone === "hot" ? 1 : 0;
        s.color = [...COLORS[p.tone]] as [number, number, number];
        fill();
        draw();
        updateStat();
      }, 200);
    }

    return () => {
      stop();
      ro.disconnect();
      io.disconnect();
      window.clearInterval(poll);
    };
  }, [reduced]);

  const Status = tone === "hot" ? WarnIcon : tone === "ok" ? CheckIcon : IdleIcon;
  const statusLabel = tone === "hot" ? "Saturated" : tone === "ok" ? "Healthy" : "Idle";

  return (
    <div className={styles.row} data-tone={tone}>
      <div className={styles.id}>
        <div className={styles.ident}>
          <span className={styles.ifaceIcon} aria-hidden="true">
            {icon}
          </span>
          <span className={styles.ifaceName}>{label}</span>
        </div>
        <span className={styles.stat}>
          <span className={styles.statValue} ref={statRef}>
            0
          </span>
          <span className={styles.statUnit}>MB/s</span>
        </span>
        <span className={styles.status}>
          <Status size={15} strokeWidth={2} aria-hidden="true" />
          <span className={styles.statusWord}>{statusLabel}</span>
        </span>
      </div>
      <div className={styles.main}>
        <div className={styles.strip}>
          <div className={styles.chips}>{chips}</div>
        </div>
        <div className={styles.plot}>
          <canvas ref={canvasRef} className={styles.canvas} />
          <span className={styles.max} aria-hidden="true">
            {max}
          </span>
        </div>
      </div>
    </div>
  );
}
