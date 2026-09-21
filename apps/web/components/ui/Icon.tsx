// One consistent line style for the whole console. Icons are decorative next to text, so
// they are hidden from assistive technology; a control with no visible label carries its
// own accessible name instead.
export type IconName =
  | "overview" | "agents" | "runs" | "approvals" | "tools" | "knowledge"
  | "evaluations" | "spend" | "workspaces" | "signOut" | "chevronRight"
  | "chevronDown" | "menu" | "close" | "light" | "dark" | "system" | "language"
  | "refresh" | "arrowRight" | "arrowLeft" | "integrations" | "catalog";

const PATHS: Record<IconName, React.ReactNode> = {
  overview: <><path d="M4 10.5 12 4l8 6.5"/><path d="M6 10v9h12v-9"/><path d="M10 19v-5h4v5"/></>,
  agents: <><rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 4v4"/><path d="M9 13h.01M15 13h.01"/><path d="M9.5 16h5"/></>,
  runs: <><circle cx="12" cy="12" r="8.5"/><path d="M10.5 9.5 15 12l-4.5 2.5z"/></>,
  approvals: <><circle cx="12" cy="12" r="8.5"/><path d="m8.5 12 2.5 2.5 4.5-5"/></>,
  tools: <><path d="M15.5 4.5a4.5 4.5 0 0 0-5.9 5.7L4 15.8 6.2 18l5.6-5.6a4.5 4.5 0 0 0 5.7-5.9l-2.6 2.6-2.1-2.1z"/></>,
  knowledge: <><ellipse cx="12" cy="6.5" rx="7" ry="2.8"/><path d="M5 6.5v11c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-11"/><path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/></>,
  evaluations: <><path d="M5 19V9"/><path d="M12 19V5"/><path d="M19 19v-6"/></>,
  spend: <><rect x="3.5" y="6" width="17" height="12" rx="2.5"/><path d="M3.5 10h17"/><path d="M7 14.5h3"/></>,
  workspaces: <><rect x="4" y="4" width="7" height="7" rx="1.8"/><rect x="13" y="4" width="7" height="7" rx="1.8"/><rect x="4" y="13" width="7" height="7" rx="1.8"/><rect x="13" y="13" width="7" height="7" rx="1.8"/></>,
  signOut: <><path d="M14 5H7a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h7"/><path d="m16 15 3-3-3-3"/><path d="M19 12h-9"/></>,
  chevronRight: <path d="m10 7 5 5-5 5"/>,
  chevronDown: <path d="m7 10 5 5 5-5"/>,
  menu: <><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/></>,
  close: <><path d="m6 6 12 12"/><path d="m18 6-12 12"/></>,
  light: <><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/></>,
  dark: <path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z"/>,
  system: <><rect x="3.5" y="5" width="17" height="11" rx="2"/><path d="M9 20h6"/><path d="M12 16v4"/></>,
  language: <><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.2 2.3 3.4 5.3 3.4 8.5s-1.2 6.2-3.4 8.5c-2.2-2.3-3.4-5.3-3.4-8.5S9.8 5.8 12 3.5z"/></>,
  refresh: <><path d="M19 12a7 7 0 1 1-2.1-5"/><path d="M19 4v4h-4"/></>,
  arrowRight: <><path d="M4 12h15"/><path d="m14 7 5 5-5 5"/></>,
  arrowLeft: <><path d="M20 12H5"/><path d="m10 17-5-5 5-5"/></>,
  integrations: <><path d="M9 3v5"/><path d="M15 3v5"/><path d="M6.5 8h11v4a5.5 5.5 0 0 1-11 0z"/><path d="M12 17.5V21"/></>,
  catalog: <><rect x="3.5" y="4.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="4.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="14" width="7" height="5.5" rx="1.8"/><path d="M13.5 14h7"/><path d="M13.5 17h7"/><path d="M13.5 20h4"/></>,
};

export function Icon({ name, size = 20, className }: {
  name: IconName; size?: number; className?: string;
}) {
  return <svg
    className={className} width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false"
  >{PATHS[name]}</svg>;
}

/** The brand mark, drawn locally so the interface never renders itself as an image. */
export function BrandMark({ size = 20 }: { size?: number }) {
  return <svg
    width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false"
  >
    <path d="M6 18V6l12 12V6"/>
  </svg>;
}
