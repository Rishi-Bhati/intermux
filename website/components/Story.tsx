"use client";

import { Fragment, useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import ModeSwitch from "./ModeSwitch";
import Monitor from "./story/Monitor";
import Pipeline from "./story/Pipeline";
import styles from "./Story.module.css";

type Segment = string | { term: string; tip: string };

type Block = {
  title: string;
  body: Segment[];
  /** Show the on/off button under the text. */
  action?: boolean;
  /** Which side of the centre line the text sits on. The diagram takes the other side. */
  side: "left" | "right";
};

const blocks: Block[] = [
  {
    side: "left",
    title: "First, with InterMux off.",
    body: [
      "Every app squeezes onto Ethernet while Wi-Fi and your phone sit idle. One big download, and your call lags.",
    ],
    action: true,
  },
  {
    side: "left",
    title: "Now, with InterMux on.",
    body: [
      "Each app gets its own lane. Downloads keep Ethernet, the browser takes Wi-Fi, the call moves to your phone.",
    ],
    action: true,
  },
  {
    side: "right",
    title: "One command builds each lane.",
    body: [
      "Each app runs in its own ",
      {
        term: "isolated network",
        tip: "A Linux network namespace: its own interfaces, routes and DNS, kept apart from the rest of your system.",
      },
      " wired to the interface you chose. Only the setup needs ",
      {
        term: "root",
        tip: "A small helper makes the network changes after one polkit password prompt. The app itself never gets root.",
      },
      ".",
    ],
  },
];

const countTokens = (body: Segment[]) =>
  body.reduce(
    (n, seg) => n + (typeof seg === "string" ? seg.split(/\s+/).filter(Boolean).length : 1),
    0,
  );

/** A paragraph split into words, so each one can light up as you read down the page. */
function Body({
  id,
  body,
  bodyRef,
}: {
  id: string;
  body: Segment[];
  bodyRef: (el: HTMLParagraphElement | null) => void;
}) {
  let index = 0;
  return (
    <p ref={bodyRef} className={styles.body} style={{ "--n": countTokens(body) } as CSSProperties}>
      {body.map((seg, s) => {
        if (typeof seg !== "string") {
          const i = index++;
          const tipId = `${id}-tip-${s}`;
          return (
            <span key={s} className={styles.word} style={{ "--i": i } as CSSProperties}>
              <button type="button" className={styles.term} aria-describedby={tipId}>
                {seg.term}
              </button>
              <span role="tooltip" id={tipId} className={styles.tip}>
                {seg.tip}
              </span>
            </span>
          );
        }
        return seg.split(/(\s+)/).map((piece, k) => {
          if (!piece) return null;
          if (/^\s+$/.test(piece)) return <Fragment key={`${s}-${k}`}>{" "}</Fragment>;
          const i = index++;
          return (
            <span key={`${s}-${k}`} className={styles.word} style={{ "--i": i } as CSSProperties}>
              {piece}
            </span>
          );
        });
      })}
    </p>
  );
}

const clamp = (n: number, lo = 0, hi = 1) => Math.min(Math.max(n, lo), hi);

export default function Story() {
  const textRef = useRef<HTMLDivElement>(null);
  const visualRefs = useRef<Array<HTMLDivElement | null>>([]);
  const blockRefs = useRef<Array<HTMLElement | null>>([]);
  const bodyRefs = useRef<Array<HTMLParagraphElement | null>>([]);

  const [active, setActive] = useState(0);
  // Until the reader uses a switch, the diagram follows the text: off for the first block, on after it.
  // Once they choose, their choice holds for the whole diagram. It used to hold only for the block that
  // was active, so a switch pressed just before the next block reached the middle of the screen undid
  // itself a moment later.
  const [choice, setChoice] = useState<boolean | null>(null);

  const stage = choice === null ? Math.min(active, 1) : choice ? 1 : 0;
  const on = stage >= 1;

  const toggle = (next: boolean) => setChoice(next);

  const update = useCallback(() => {
    const vh = window.innerHeight;
    const stacked = window.innerWidth <= 1100;
    // Stacked, a diagram is pinned at the top; the reading line is the middle of what is left below it.
    let visualBottom = 0;
    visualRefs.current.forEach((el) => {
      if (!el) return;
      const r = el.getBoundingClientRect();
      if (r.top <= 1 && r.bottom > 0) visualBottom = Math.max(visualBottom, r.bottom);
    });
    // The line the reader is "at": the middle of the screen, or of the space under the pinned diagram.
    const focusY = stacked ? (visualBottom + vh) / 2 : vh / 2;

    let best = 0;
    let bestDist = Infinity;
    const centers: number[] = [];
    blockRefs.current.forEach((el, i) => {
      if (!el) return;
      const r = el.getBoundingClientRect();
      const center = r.top + r.height / 2;
      centers[i] = center;
      const dist = Math.abs(center - focusY);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    });
    setActive((prev) => (prev === best ? prev : best));

    // Words light up as each paragraph travels up through the reading line.
    bodyRefs.current.forEach((el) => {
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      const p = clamp((focusY + vh * 0.26 - top) / (vh * 0.3));
      el.style.setProperty("--p", p.toFixed(3));
    });

    if (textRef.current && centers.length > 1) {
      const first = centers[0];
      const last = centers[centers.length - 1];
      const rp = clamp((focusY - first) / (last - first));
      textRef.current.style.setProperty("--rp", rp.toFixed(3));
    }
  }, []);

  useEffect(() => {
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    };
    frame = requestAnimationFrame(update);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [update]);

  const renderBlock = (block: Block, i: number) => (
    <article
      key={block.title}
      ref={(el) => {
        blockRefs.current[i] = el;
      }}
      className={styles.block}
      data-side={block.side}
      data-active={active === i || undefined}
      data-passed={active > i || undefined}
    >
      <h2 className={styles.title}>{block.title}</h2>
      <Body
        id={`story-${i}`}
        body={block.body}
        bodyRef={(el) => {
          bodyRefs.current[i] = el;
        }}
      />
      {block.action && (
        // A real toggle: its label always says what pressing it will do now. It used to be fixed
        // ("Turn InterMux on" in the first block), so pressing it again could never turn InterMux off.
        <button type="button" className={styles.action} onClick={() => toggle(!on)} aria-pressed={on}>
          <span
            className={styles.actionIcon}
            data-on={on || undefined}
            data-target={on ? "off" : "on"}
            aria-hidden="true"
          />
          {on ? "Turn InterMux off" : "Turn InterMux on"}
        </button>
      )}
    </article>
  );

  return (
    <section className={styles.story} aria-label="Why you need InterMux and how it works">
      <div className={styles.inner}>
        <div ref={textRef} className={styles.text}>
          <div className={styles.rail} aria-hidden="true">
            <span className={styles.railFill} />
          </div>

          {/* Part one: the text on the left, the live monitor pinned on the right until the last of its blocks has passed. */}
          <div className={styles.part}>
            <div className={styles.copy}>{blocks.slice(0, 2).map((b, i) => renderBlock(b, i))}</div>
            <div
              ref={(el) => {
                visualRefs.current[0] = el;
              }}
              className={styles.visual}
              data-side="right"
            >
              <div className={styles.panel}>
                <Monitor stage={stage} />
              </div>
              <ModeSwitch on={on} onChange={toggle} />
            </div>
          </div>

          {/* Part two: a new diagram on the other side, scrolling up as the monitor leaves. */}
          <div className={styles.part}>
            <div
              ref={(el) => {
                visualRefs.current[1] = el;
              }}
              className={styles.visual}
              data-side="left"
            >
              <div className={styles.panel}>
                <Pipeline />
              </div>
            </div>
            <div className={styles.copy}>{renderBlock(blocks[2], 2)}</div>
          </div>
        </div>
      </div>
    </section>
  );
}
