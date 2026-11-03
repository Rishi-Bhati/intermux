"use client";

import {
  Code,
  Globe,
  HardDriveDownload,
  Music,
  RotateCcw,
  Router,
  Smartphone,
  Video,
  Wifi,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import styles from "./Switchboard.module.css";

type IfaceId = "eth0" | "wlan0" | "usb0";

// `cap` is what the link carries and `load` what each app wants, in the same made-up units.
const IFACES: Array<{ id: IfaceId; label: string; cap: number; icon: ReactNode }> = [
  { id: "eth0", label: "Ethernet", cap: 80, icon: <Router size={18} strokeWidth={1.8} /> },
  { id: "wlan0", label: "Wi-Fi", cap: 40, icon: <Wifi size={18} strokeWidth={1.8} /> },
  { id: "usb0", label: "Phone", cap: 12, icon: <Smartphone size={18} strokeWidth={1.8} /> },
];

const APPS: Array<{ id: string; load: number; color: string; icon: ReactNode }> = [
  { id: "qbittorrent", load: 68, color: "var(--amber)", icon: <HardDriveDownload size={15} strokeWidth={2} /> },
  { id: "firefox", load: 16, color: "var(--cyan)", icon: <Globe size={15} strokeWidth={2} /> },
  { id: "zoom", load: 7, color: "var(--violet)", icon: <Video size={15} strokeWidth={2} /> },
  { id: "spotify", load: 4, color: "var(--green)", icon: <Music size={15} strokeWidth={2} /> },
  { id: "code", load: 3, color: "var(--pink)", icon: <Code size={15} strokeWidth={2} /> },
];

/** Without InterMux, Linux sends everything out of the default route. */
const DEFAULT: Record<string, IfaceId> = Object.fromEntries(APPS.map((a) => [a.id, "eth0"]));

type Pt = { x: number; y: number };
type Layout = { plugs: Record<string, Pt>; sockets: Record<IfaceId, { x: number; top: number; h: number }> };

const curve = (a: Pt, b: Pt) => {
  const dx = Math.max(40, (b.x - a.x) * 0.5);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
};

const status = (pct: number) => (pct === 0 ? "idle" : pct > 1 ? "jammed" : pct > 0.8 ? "busy" : "ok");
const STATUS_TEXT = { idle: "Idle", ok: "Clear", busy: "Busy", jammed: "Jammed" } as const;

export default function Switchboard() {
  const [bind, setBind] = useState<Record<string, IfaceId>>(DEFAULT);
  const [last, setLast] = useState<{ app: string; iface: IfaceId; n: number } | null>(null);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [drag, setDrag] = useState<{ app: string; at: Pt; over: IfaceId | null } | null>(null);

  const boardRef = useRef<HTMLDivElement>(null);
  const plugRefs = useRef<Record<string, HTMLElement | null>>({});
  const socketRefs = useRef<Record<string, HTMLElement | null>>({});
  const press = useRef<{ app: string; start: Pt; moved: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  const measure = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    const b = board.getBoundingClientRect();
    const plugs: Layout["plugs"] = {};
    for (const a of APPS) {
      const r = plugRefs.current[a.id]?.getBoundingClientRect();
      if (r) plugs[a.id] = { x: r.left + r.width / 2 - b.left, y: r.top + r.height / 2 - b.top };
    }
    const sockets = {} as Layout["sockets"];
    for (const f of IFACES) {
      const r = socketRefs.current[f.id]?.getBoundingClientRect();
      if (r) sockets[f.id] = { x: r.left + r.width / 2 - b.left, top: r.top - b.top, h: r.height };
    }
    setLayout({ plugs, sockets });
  }, []);

  useLayoutEffect(() => {
    measure();
    const ro = new ResizeObserver(measure);
    if (boardRef.current) ro.observe(boardRef.current);
    return () => ro.disconnect();
  }, [measure]);

  const loads = Object.fromEntries(
    IFACES.map((f) => [f.id, APPS.reduce((n, a) => n + (bind[a.id] === f.id ? a.load : 0), 0)]),
  ) as Record<IfaceId, number>;
  const jammed = IFACES.some((f) => loads[f.id] > f.cap);
  const touched = Object.keys(bind).some((k) => bind[k] !== DEFAULT[k]);

  const move = (app: string, iface: IfaceId) => {
    if (bind[app] === iface) return;
    setBind((b) => ({ ...b, [app]: iface }));
    setLast((l) => ({ app, iface, n: (l?.n ?? 0) + 1 }));
  };

  const cycle = (app: string) => {
    const i = IFACES.findIndex((f) => f.id === bind[app]);
    move(app, IFACES[(i + 1) % IFACES.length].id);
  };

  // Where a cable lands in a socket: the cables sharing a socket fan out across its height.
  const socketPoint = (iface: IfaceId, app: string): Pt | null => {
    const s = layout?.sockets[iface];
    if (!s) return null;
    const on = APPS.filter((a) => bind[a.id] === iface && a.id !== drag?.app);
    const k = Math.max(0, on.findIndex((a) => a.id === app));
    const n = Math.max(1, on.length);
    return { x: s.x, y: s.top + (s.h * (k + 1)) / (n + 1) };
  };

  const local = (e: { clientX: number; clientY: number }): Pt => {
    const b = boardRef.current!.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  };

  const ifaceAt = (e: { clientX: number; clientY: number }): IfaceId | null => {
    const el = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-iface]");
    return (el?.dataset.iface as IfaceId | undefined) ?? null;
  };

  const onDown = (app: string) => (e: ReactPointerEvent) => {
    if (e.button !== 0) return;
    press.current = { app, start: { x: e.clientX, y: e.clientY }, moved: false, id: e.pointerId };
  };

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const p = press.current;
      if (!p || e.pointerId !== p.id) return;
      if (!p.moved && Math.hypot(e.clientX - p.start.x, e.clientY - p.start.y) < 6) return;
      p.moved = true;
      e.preventDefault();
      setDrag({ app: p.app, at: local(e), over: ifaceAt(e) });
    };
    const onUp = (e: PointerEvent) => {
      const p = press.current;
      if (!p || e.pointerId !== p.id) return;
      press.current = null;
      if (!p.moved) return;
      suppressClick.current = true;
      setTimeout(() => (suppressClick.current = false), 0);
      const target = ifaceAt(e);
      if (target) move(p.app, target);
      setDrag(null);
    };
    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  });

  const command = last
    ? `intermux adopt --app ${last.app} --iface ${last.iface}`
    : "# every app is on eth0. drag a cable to move one.";

  return (
    <section className={styles.section} aria-labelledby="switchboard-title">
      <div className={styles.head}>
        <span className="eyebrow">Try it</span>
        <h2 id="switchboard-title" className="section-title">
          Plug any app into any network.
        </h2>
      </div>

      <div className={styles.frame} data-clear={(touched && !jammed) || undefined}>
        <div ref={boardRef} className={styles.board} data-dragging={drag ? true : undefined} data-fresh={!touched || undefined}>
          <svg className={styles.wires} aria-hidden="true">
            {layout &&
              APPS.map((a) => {
                const from = layout.plugs[a.id];
                const dragging = drag?.app === a.id;
                const to = dragging ? drag.at : socketPoint(bind[a.id], a.id);
                if (!from || !to) return null;
                const d = curve(from, to);
                const st = dragging ? "ok" : status(loads[bind[a.id]] / IFACES.find((f) => f.id === bind[a.id])!.cap);
                const style = { "--c": a.color, d: `path("${d}")` } as CSSProperties;
                return (
                  <g key={a.id} data-status={st} data-dragging={dragging || undefined}>
                    <path className={styles.wire} d={d} style={style} />
                    <path className={styles.flow} d={d} style={style} />
                  </g>
                );
              })}
          </svg>

          <ul className={styles.apps} aria-label="Apps">
            {APPS.map((a) => {
              const iface = IFACES.find((f) => f.id === bind[a.id])!;
              const lag = loads[iface.id] > iface.cap;
              return (
                <li key={a.id}>
                  <button
                    type="button"
                    className={styles.app}
                    style={{ "--c": a.color } as CSSProperties}
                    data-lag={lag || undefined}
                    data-lifted={drag?.app === a.id || undefined}
                    onPointerDown={onDown(a.id)}
                    onClick={() => !suppressClick.current && cycle(a.id)}
                    aria-label={`${a.id} uses ${iface.label}${lag ? " and is lagging" : ""}. Press to move it to the next network.`}
                  >
                    <span className={styles.appIcon}>{a.icon}</span>
                    <span className={styles.appName}>{a.id}</span>
                    {lag && <span className={styles.lag}>lag</span>}
                    <span
                      ref={(el) => {
                        plugRefs.current[a.id] = el;
                      }}
                      className={styles.plug}
                    />
                  </button>
                </li>
              );
            })}
          </ul>

          <ul className={styles.ifaces} aria-label="Networks">
            {IFACES.map((f) => {
              const pct = loads[f.id] / f.cap;
              const st = status(pct);
              return (
                <li
                  key={f.id}
                  className={styles.iface}
                  data-iface={f.id}
                  data-status={st}
                  data-over={drag?.over === f.id || undefined}
                >
                  <span
                    ref={(el) => {
                      socketRefs.current[f.id] = el;
                    }}
                    className={styles.socket}
                  />
                  <div className={styles.ifaceTop}>
                    <span className={styles.ifaceIcon}>{f.icon}</span>
                    <span className={styles.ifaceName}>
                      {f.label}
                      <code>{f.id}</code>
                    </span>
                    <span className={styles.state}>{STATUS_TEXT[st]}</span>
                  </div>
                  <div className={styles.meter} aria-hidden="true">
                    <span style={{ width: `${Math.min(pct, 1) * 100}%` }} />
                  </div>
                </li>
              );
            })}
          </ul>
        </div>

        <div className={styles.console}>
          <code key={last?.n ?? 0} className={styles.cmd} style={{ "--n": command.length + 2 } as CSSProperties}>
            <span className={styles.dollar}>{last ? "$" : ""}</span> {command}
          </code>
          <span className={styles.verdict} role="status">
            {touched && !jammed ? "Nothing jammed. Every app has room." : ""}
          </span>
          {touched && (
            <button
              type="button"
              className={styles.reset}
              onClick={() => {
                setBind(DEFAULT);
                setLast(null);
              }}
            >
              <RotateCcw size={13} strokeWidth={2.2} aria-hidden="true" />
              Reset
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
