import { ImageResponse } from "next/og";

export const alt = "InterMux: pick a network for every app on Linux";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const lanes = [
  { app: "qbittorrent", iface: "Ethernet", color: "#ffb547" },
  { app: "firefox", iface: "Wi-Fi", color: "#38c8f0" },
  { app: "zoom", iface: "Tethering", color: "#a78bfa" },
];

/** The share card: the headline on the left, three apps each on their own connection on the right. */
export default function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          background: "#0c0d0e",
          color: "#e9eaeb",
          padding: 72,
          fontFamily: "sans-serif",
          backgroundImage:
            "linear-gradient(to right, rgba(255,255,255,0.04) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,0.04) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 34, fontWeight: 600 }}>
            <svg width="48" height="48" viewBox="0 0 64 64" fill="none">
              <path d="M12 52V12M52 52V12" stroke="#868b91" strokeWidth="8" strokeLinecap="round" />
              <path d="M12 12L32 36L52 12" stroke="#3a5aff" strokeWidth="8" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="12" cy="12" r="8" fill="#3a5aff" />
              <circle cx="52" cy="12" r="8" fill="#3a5aff" />
              <circle cx="32" cy="36" r="9.5" fill="#3a5aff" />
              <rect x="4" y="44" width="16" height="16" rx="3.5" fill="#e9eaeb" />
              <rect x="44" y="44" width="16" height="16" rx="3.5" fill="#e9eaeb" />
            </svg>
            InterMux
          </div>
          <div style={{ display: "flex", flexDirection: "column", fontSize: 76, fontWeight: 700, lineHeight: 1.02, letterSpacing: -3 }}>
            <span>Pick a network</span>
            <span style={{ color: "#868b91" }}>for every app.</span>
          </div>
          <div style={{ display: "flex", fontSize: 26, color: "#868b91" }}>
            Free and open source · for Linux
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", justifyContent: "center", gap: 22, width: 430 }}>
          {lanes.map((l) => (
            <div
              key={l.app}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "22px 26px",
                background: "#121315",
                border: "1px solid #33363a",
                borderLeft: `4px solid ${l.color}`,
                borderRadius: 6,
                fontSize: 26,
              }}
            >
              <span style={{ display: "flex", color: l.color, fontFamily: "monospace" }}>{l.app}</span>
              <span style={{ display: "flex", color: "#868b91" }}>→ {l.iface}</span>
            </div>
          ))}
        </div>
      </div>
    ),
    size,
  );
}
