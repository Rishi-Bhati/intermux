"use client";

import { useEffect, useRef } from "react";
import styles from "./Globe.module.css";

/*
 * A large network globe rising from the bottom edge of the hero. It turns very slowly (one
 * revolution in about two and a half minutes). A few nodes breathe in the accent colour and a
 * handful of packets hop from node to node. Everything is drawn at low contrast on purpose.
 */

const POINTS = 240;
const NEIGHBOURS = 3;
const ACCENT_EVERY = 31;
const PACKETS = 4;
const SPIN = 0.04; // radians per second
const TILT = 0.42;
const ACCENT = [58, 90, 255] as const;
/** The breathing nodes take turns in the site's supporting colours. */
const NODE_COLORS = [ACCENT, [63, 214, 154], [167, 139, 250], [56, 200, 240], [255, 181, 71]] as const;

type Vec = { x: number; y: number; z: number };

function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Points spread evenly over a sphere (Fibonacci lattice), nudged a little so it does not look like a grid.
function buildPoints(): Vec[] {
  const rand = mulberry32(7);
  const golden = Math.PI * (3 - Math.sqrt(5));
  return Array.from({ length: POINTS }, (_, i) => {
    const y = 1 - (i / (POINTS - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i + (rand() - 0.5) * 0.25;
    return { x: Math.cos(theta) * r, y, z: Math.sin(theta) * r };
  });
}

function buildEdges(points: Vec[]) {
  const seen = new Set<string>();
  const edges: Array<[number, number]> = [];
  const adjacency: number[][] = points.map(() => []);
  points.forEach((a, i) => {
    points
      .map((b, j) => ({ j, d: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2 }))
      .filter((o) => o.j !== i)
      .sort((p, q) => p.d - q.d)
      .slice(0, NEIGHBOURS)
      .forEach(({ j }) => {
        const key = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (!seen.has(key)) {
          seen.add(key);
          edges.push([i, j]);
        }
        if (!adjacency[i].includes(j)) adjacency[i].push(j);
        if (!adjacency[j].includes(i)) adjacency[j].push(i);
      });
  });
  return { edges, adjacency };
}

const points = buildPoints();
const { edges, adjacency } = buildEdges(points);
const accentNodes = points.map((_, i) => i).filter((i) => i % ACCENT_EVERY === 3);

type Packet = { from: number; to: number; t: number; speed: number };

export default function Globe() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const rand = mulberry32(99);
    const packets: Packet[] = Array.from({ length: PACKETS }, () => {
      const from = Math.floor(rand() * POINTS);
      const to = adjacency[from][Math.floor(rand() * adjacency[from].length)];
      return { from, to, t: rand(), speed: 0.22 + rand() * 0.18 };
    });

    let width = 0;
    let height = 0;
    let dpr = 1;
    let raf = 0;
    let last = performance.now();
    let angle = 1.1;
    let clock = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    };

    const screen = new Float32Array(POINTS * 3);

    const draw = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      const radius = Math.min(Math.max(width * 0.62, 260), height * 0.56);
      const cx = width / 2;
      const cy = height * 1.13;

      // A barely-there body, so the globe reads as a solid without a hard edge.
      const body = ctx.createRadialGradient(cx, cy - radius * 0.7, radius * 0.1, cx, cy, radius);
      body.addColorStop(0, "rgba(255,255,255,0.035)");
      body.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = "rgba(255,255,255,0.07)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.stroke();

      // Rotate (spin about the vertical axis, then tilt toward the viewer) and project.
      const cosA = Math.cos(angle);
      const sinA = Math.sin(angle);
      const cosT = Math.cos(TILT);
      const sinT = Math.sin(TILT);
      for (let i = 0; i < POINTS; i++) {
        const p = points[i];
        const x1 = p.x * cosA + p.z * sinA;
        const z1 = -p.x * sinA + p.z * cosA;
        const y2 = p.y * cosT - z1 * sinT;
        const z2 = p.y * sinT + z1 * cosT;
        const k = 1 + z2 * 0.06;
        screen[i * 3] = cx + x1 * radius * k;
        screen[i * 3 + 1] = cy - y2 * radius * k;
        screen[i * 3 + 2] = z2;
      }

      // Links
      ctx.lineWidth = 1;
      for (const [a, b] of edges) {
        const depth = (screen[a * 3 + 2] + screen[b * 3 + 2]) / 2;
        const front = (depth + 1) / 2;
        const alpha = 0.03 + front * front * 0.2;
        ctx.strokeStyle = `rgba(255,255,255,${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(screen[a * 3], screen[a * 3 + 1]);
        ctx.lineTo(screen[b * 3], screen[b * 3 + 1]);
        ctx.stroke();
      }

      // Nodes
      for (let i = 0; i < POINTS; i++) {
        const front = (screen[i * 3 + 2] + 1) / 2;
        const alpha = 0.08 + front * front * 0.6;
        ctx.fillStyle = `rgba(255,255,255,${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(screen[i * 3], screen[i * 3 + 1], 0.9 + front * 1.1, 0, Math.PI * 2);
        ctx.fill();
      }

      // Accent nodes breathe slowly.
      for (const [k, i] of accentNodes.entries()) {
        const front = (screen[i * 3 + 2] + 1) / 2;
        const pulse = 0.5 + 0.5 * Math.sin(clock * 0.9 + i);
        const alpha = (0.1 + 0.55 * pulse) * (0.25 + 0.75 * front);
        const x = screen[i * 3];
        const y = screen[i * 3 + 1];
        ctx.fillStyle = `rgba(${NODE_COLORS[k % NODE_COLORS.length].join(",")},${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(x, y, 1.8 + front, 0, Math.PI * 2);
        ctx.fill();
      }

      // Packets hopping along links
      for (const pk of packets) {
        const ax = screen[pk.from * 3];
        const ay = screen[pk.from * 3 + 1];
        const bx = screen[pk.to * 3];
        const by = screen[pk.to * 3 + 1];
        const depth = (screen[pk.from * 3 + 2] + screen[pk.to * 3 + 2]) / 2;
        const front = (depth + 1) / 2;
        const fade = Math.sin(Math.PI * Math.min(Math.max(pk.t, 0), 1));
        const alpha = front * front * 0.9 * fade;
        if (alpha < 0.02) continue;
        ctx.fillStyle = `rgba(${ACCENT.join(",")},${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(ax + (bx - ax) * pk.t, ay + (by - ay) * pk.t, 1.6, 0, Math.PI * 2);
        ctx.fill();
      }

    };

    const step = (dt: number) => {
      angle += SPIN * dt;
      clock += dt;
      for (const pk of packets) {
        pk.t += pk.speed * dt;
        if (pk.t >= 1) {
          const next = adjacency[pk.to];
          const options = next.filter((n) => n !== pk.from);
          const pool = options.length ? options : next;
          pk.from = pk.to;
          pk.to = pool[Math.floor(rand() * pool.length)];
          pk.t = 0;
        }
      }
    };

    const frame = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.1);
      last = now;
      step(dt);
      draw();
      raf = requestAnimationFrame(frame);
    };

    const start = () => {
      cancelAnimationFrame(raf);
      resize();
      if (motionQuery.matches) {
        draw(); // a single still frame
      } else {
        last = performance.now();
        raf = requestAnimationFrame(frame);
      }
    };

    start();
    const observer = new ResizeObserver(start);
    observer.observe(canvas);
    motionQuery.addEventListener("change", start);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      motionQuery.removeEventListener("change", start);
    };
  }, []);

  return <canvas ref={ref} className={styles.canvas} aria-hidden="true" />;
}
