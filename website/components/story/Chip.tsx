"use client";

import { useEffect, useLayoutEffect, useRef, type CSSProperties, type ReactNode } from "react";
import usePrefersReducedMotion from "@/hooks/usePrefersReducedMotion";
import styles from "./Monitor.module.css";

type Point = { x: number; y: number };

/**
 * Where each chip is currently seen, measured inside the panel so scrolling the page never matters.
 * It is kept fresh while a chip is still, and every frame while a chip is moving. It is never
 * measured while React is removing elements, because by then neighbours have already shifted.
 */
const lastSeen = new Map<string, Point>();

const MOVE_MS = 1100;
const STAGGER_MS = 120;

function position(el: HTMLElement): Point | null {
  const root = el.closest("[data-monitor]");
  if (!root) return null;
  const box = el.getBoundingClientRect();
  if (box.width === 0) return null;
  const origin = root.getBoundingClientRect();
  return { x: box.left - origin.left, y: box.top - origin.top };
}

/**
 * An app chip. When it lands somewhere new (another connection, or nudged along by a neighbour
 * leaving) it glides there from where it was last seen, rising slightly on the way.
 */
export default function Chip({
  id,
  label,
  icon,
  color,
  order = 0,
}: {
  id: string;
  label: string;
  icon: ReactNode;
  /** Colour of the icon, so each app is easy to follow as it moves. */
  color?: string;
  /** Position in the group, used to stagger the moves. */
  order?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const reduced = usePrefersReducedMotion();

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Stop any move in progress so the resting position can be measured.
    el.getAnimations().forEach((a) => a.cancel());
    const rest = position(el);
    if (!rest) return;
    const from = lastSeen.get(id);
    const dx = from ? from.x - rest.x : 0;
    const dy = from ? from.y - rest.y : 0;
    if (!from || reduced || (Math.abs(dx) < 1 && Math.abs(dy) < 1)) {
      lastSeen.set(id, rest);
      return;
    }
    // Moving: leave the remembered spot alone. The tracker below updates it frame by frame. (In
    // development React runs this effect twice for a new element; the second run must still see
    // where the chip really came from.)

    const timing = { duration: MOVE_MS, delay: order * STAGGER_MS, fill: "backwards" as const };
    el.setAttribute("data-moving", "");
    // One smooth glide, plus a separate gentle rise, so the motion never stops halfway.
    const glide = el.animate(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "translate(0, 0)" }],
      { ...timing, easing: "cubic-bezier(0.5, 0, 0.15, 1)" },
    );
    el.animate([{ scale: "1" }, { scale: "1.1" }, { scale: "1" }], { ...timing, easing: "ease-in-out" });

    // Keep recording where the chip visibly is, so a quick second switch can pick up from there.
    let frame = 0;
    const track = () => {
      const here = position(el);
      if (here) lastSeen.set(id, here);
      if (el.isConnected && glide.playState !== "finished" && glide.playState !== "idle") {
        frame = requestAnimationFrame(track);
      }
    };
    frame = requestAnimationFrame(track);

    const done = () => {
      el.removeAttribute("data-moving");
      cancelAnimationFrame(frame);
      const final = position(el);
      if (final) lastSeen.set(id, final);
    };
    glide.onfinish = done;
    glide.oncancel = () => {
      el.removeAttribute("data-moving");
      cancelAnimationFrame(frame);
    };
  });

  // Keep the "last seen" spot right when nothing is moving but the layout changes (resize, fonts loading).
  useEffect(() => {
    const el = ref.current;
    const root = el?.closest("[data-monitor]");
    if (!el || !root) return;
    const refresh = () => {
      if (el.getAnimations().length) return;
      const here = position(el);
      if (here) lastSeen.set(id, here);
    };
    const observer = new ResizeObserver(refresh);
    observer.observe(root);
    observer.observe(el);
    if (el.parentElement) observer.observe(el.parentElement);
    document.fonts?.ready.then(refresh);
    return () => observer.disconnect();
  }, [id]);

  return (
    <span ref={ref} className={styles.chip} style={color ? ({ "--icon": color } as CSSProperties) : undefined}>
      {icon}
      {label}
    </span>
  );
}
