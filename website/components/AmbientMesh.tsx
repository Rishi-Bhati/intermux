import styles from "./AmbientMesh.module.css";

/**
 * A very quiet field of nodes behind the hero. Generated from a fixed seed, so the server and
 * the browser always draw the same mesh.
 */
const W = 1440;
const H = 900;
const COUNT = 46;

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

const r1 = (n: number) => Math.round(n * 10) / 10;

const rand = mulberry32(2026);
const nodes = Array.from({ length: COUNT }, () => ({
  x: r1(rand() * W),
  y: r1(rand() * H),
  pulse: rand() < 0.16,
  delay: r1(rand() * 6),
}));

const links: Array<[number, number]> = [];
{
  const seen = new Set<string>();
  nodes.forEach((a, i) => {
    nodes
      .map((b, j) => ({ j, d: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 }))
      .filter((o) => o.j !== i)
      .sort((p, q) => p.d - q.d)
      .slice(0, 2)
      .forEach(({ j }) => {
        const key = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (!seen.has(key)) {
          seen.add(key);
          links.push([i, j]);
        }
      });
  });
}

export default function AmbientMesh() {
  return (
    <svg
      className={styles.mesh}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      <g className={styles.links}>
        {links.map(([i, j]) => (
          <line key={`${i}-${j}`} x1={nodes[i].x} y1={nodes[i].y} x2={nodes[j].x} y2={nodes[j].y} />
        ))}
      </g>
      <g>
        {nodes.map((n, i) => (
          <circle
            key={i}
            cx={n.x}
            cy={n.y}
            r={n.pulse ? 2.4 : 1.7}
            className={n.pulse ? styles.pulse : styles.dot}
            style={n.pulse ? { animationDelay: `${n.delay}s` } : undefined}
          />
        ))}
      </g>
    </svg>
  );
}
