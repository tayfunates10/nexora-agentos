import { redirect } from "next/navigation";
import { ApiError } from "../../../lib/server/api";
import {
  RUN_STATUS_TONES, runStatusKey, type RunStatus,
} from "../../../lib/agent-contracts";
import { ConsoleShell, type ConsoleChrome, type NavKey } from "../../../components/shell/ConsoleShell.tsx";
import { EmptyState, Notice, StatusBadge } from "../../../components/ui/primitives.tsx";

/**
 * Every console page distinguishes "you may not see this" from "the platform is not
 * answering". An expired session always returns to sign-in instead of showing an error,
 * and the frame stays in place so the reader can navigate away from the failure.
 */
export function ConsoleProblem({ chrome, error, area, active, back }: {
  chrome: ConsoleChrome;
  error: unknown;
  area: string;
  active?: NavKey;
  back: { href: string; label: string };
}) {
  if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
  const { ui } = chrome;
  const denied = error instanceof ApiError && [403, 404].includes(error.status);
  return <ConsoleShell chrome={chrome} active={active}>
    <h1 className="page-title">
      {ui.t(denied ? "errors.notFoundOrDenied" : "errors.unavailable", { area })}
    </h1>
    <Notice live="alert" tone="danger">
      {ui.t(denied ? "errors.deniedBody" : "errors.unavailableBody")}
    </Notice>
    <p><a className="link" href={back.href}>{back.label}</a></p>
  </ConsoleShell>;
}

export function InvalidLinkPage({ chrome, active, back }: {
  chrome: ConsoleChrome; active?: NavKey; back: { href: string; label: string };
}) {
  const { ui } = chrome;
  return <ConsoleShell chrome={chrome} active={active}>
    <h1 className="page-title">{ui.t("common.invalidLinkTitle")}</h1>
    <Notice live="alert" tone="danger">{ui.t("common.invalidLinkBody")}</Notice>
    <p><a className="link" href={back.href}>{back.label}</a></p>
  </ConsoleShell>;
}

export function RunStatusBadge({ chrome, status, suffix }: {
  chrome: ConsoleChrome; status: RunStatus; suffix?: string;
}) {
  return <StatusBadge tone={RUN_STATUS_TONES[status]}>
    {chrome.ui.t(runStatusKey(status))}{suffix ? ` · ${suffix}` : ""}
  </StatusBadge>;
}

export function Saved({ message }: { message: string | null }) {
  return message ? <Notice live="status" tone="success">{message}</Notice> : null;
}

export function Failed({ message }: { message: string | null }) {
  return message ? <Notice live="alert" tone="danger">{message}</Notice> : null;
}

export { EmptyState };
