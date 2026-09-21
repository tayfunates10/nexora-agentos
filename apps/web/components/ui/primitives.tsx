import { Icon, type IconName } from "./Icon.tsx";

export type Tone = "up" | "down" | "warn" | "neutral";

export function PageHeader({ eyebrow, title, intro, actions }: {
  eyebrow?: string; title: string; intro?: string; actions?: React.ReactNode;
}) {
  return <header className="page-header enter">
    {eyebrow && <p className="eyebrow">{eyebrow}</p>}
    <h1 className="page-title">{title}</h1>
    {intro && <p className="page-intro">{intro}</p>}
    {actions}
  </header>;
}

export function SectionHead({ id, title, action }: {
  id?: string; title: string; action?: React.ReactNode;
}) {
  return <div className="section-head">
    <h2 id={id} className="section-title">{title}</h2>
    {action}
  </div>;
}

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <article className={"card" + (className ? " " + className : "")}>{children}</article>;
}

export function Panel({ className, labelledBy, label, testId, children }: {
  className?: string;
  labelledBy?: string;
  /** For a region with no visible heading of its own. */
  label?: string;
  testId?: string;
  children: React.ReactNode;
}) {
  return <section
    className={"card panel" + (className ? " " + className : "")}
    aria-labelledby={labelledBy}
    aria-label={label}
    data-testid={testId}
  >{children}</section>;
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return <div className="empty"><h3 className="section-title">{title}</h3>{children}</div>;
}

/**
 * A result the reader must not miss stays in the page flow. Errors and confirmations are
 * announced, never parked in a toast that disappears before it is read.
 */
export function Notice({ tone, live, boxed = true, children }: {
  tone?: "success" | "warning" | "danger";
  live?: "alert" | "status";
  boxed?: boolean;
  children: React.ReactNode;
}) {
  return <p
    className={"notice" + (boxed ? " boxed" : "") + (tone ? " " + tone : "")}
    role={live}
  >{children}</p>;
}

export function Hint({ id, children }: { id?: string; children: React.ReactNode }) {
  return <p id={id} className="notice">{children}</p>;
}

export function Badge({ accent, children }: { accent?: boolean; children: React.ReactNode }) {
  return <span className={"badge" + (accent ? " accent" : "")}>{children}</span>;
}

/** Colour is an accent on the written state, never the state itself. */
export function StatusBadge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <p className={`status tone-${tone}`}>{children}</p>;
}

export function Metrics({ children }: { children: React.ReactNode }) {
  return <dl className="metrics">{children}</dl>;
}

export function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="metric"><dt>{label}</dt><dd>{value}</dd></div>;
}

export function DetailList({ children }: { children: React.ReactNode }) {
  return <dl className="details-list">{children}</dl>;
}

export function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><dt>{label}</dt><dd>{children}</dd></div>;
}

export function Field({ id, label, help, children }: {
  id: string; label: string; help?: React.ReactNode; children: React.ReactNode;
}) {
  return <div className="field">
    <label className="field-label" htmlFor={id}>{label}</label>
    {children}
    {help && <p className="field-help" id={`${id}-help`}>{help}</p>}
  </div>;
}

export function Filters({ label, children }: { label: string; children: React.ReactNode }) {
  return <nav className="filters" aria-label={label}>{children}</nav>;
}

export function FilterLink({ href, current, children }: {
  href: string; current: boolean; children: React.ReactNode;
}) {
  return <a className="filter" href={href} aria-current={current ? "page" : undefined}>{children}</a>;
}

export function Pagination({ label, previous, next }: {
  label: string;
  previous?: { href: string; label: string } | null;
  next?: { href: string; label: string } | null;
}) {
  if (!previous && !next) return null;
  return <nav className="pagination" aria-label={label}>
    {previous
      ? <a className="button secondary" href={previous.href}>
        <Icon name="arrowLeft" size={18}/>{previous.label}</a>
      : <span/>}
    {next && <a className="button secondary" href={next.href}>
      {next.label}<Icon name="arrowRight" size={18}/></a>}
  </nav>;
}

/**
 * Stored JSON, model output and worker payloads are rendered as inert text. Nothing here
 * is ever interpreted as markup or as a link.
 */
export function CodeBlock({ label, children }: { label?: string; children: string }) {
  return <pre className="codeblock" aria-label={label} tabIndex={0}>{children}</pre>;
}

export function Disclosure({ summary, open, children }: {
  summary: React.ReactNode; open?: boolean; children: React.ReactNode;
}) {
  return <details className="disclosure" open={open}>
    <summary>{summary}</summary>
    <div className="disclosure-body">{children}</div>
  </details>;
}

export function ModuleCard({ href, icon, title, detail, wide, delayMs }: {
  href: string; icon: IconName; title: string; detail: string; wide?: boolean; delayMs?: number;
}) {
  // One link with no nested controls, so the whole card is a single keyboard target. The
  // entrance is a CSS animation, so it always settles on the visible frame.
  return <a
    className={"module-card enter" + (wide ? "" : " span-2")}
    href={href}
    style={delayMs ? { animationDelay: `${delayMs}ms` } : undefined}
  >
    <span className="icon-box"><Icon name={icon} size={26}/></span>
    <span className="module-card-body">
      <span className="module-card-title">{title}</span>
      <span className="module-card-detail">{detail}</span>
    </span>
    <span className="module-card-arrow"><Icon name="chevronRight" size={18}/></span>
  </a>;
}

export function RefreshLink({ href, label }: { href: string; label: string }) {
  return <a className="button secondary small" href={href}>
    <Icon name="refresh" size={16}/>{label}
  </a>;
}
