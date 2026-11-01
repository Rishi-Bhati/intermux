/** The InterMux mark: an M drawn as a network. The blue V is the route you picked. */
export default function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <path d="M12 52V12M52 52V12" stroke="#868b91" strokeWidth="8" strokeLinecap="round" />
      <path d="M12 12L32 36L52 12" stroke="#3a5aff" strokeWidth="8" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="12" cy="12" r="8" fill="#3a5aff" />
      <circle cx="52" cy="12" r="8" fill="#3a5aff" />
      <circle cx="32" cy="36" r="9.5" fill="#3a5aff" />
      <rect x="4" y="44" width="16" height="16" rx="3.5" fill="#e9eaeb" />
      <rect x="44" y="44" width="16" height="16" rx="3.5" fill="#e9eaeb" />
    </svg>
  );
}
