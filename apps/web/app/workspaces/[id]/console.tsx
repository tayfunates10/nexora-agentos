import { redirect } from "next/navigation";
import { ApiError } from "../../../lib/server/api";
import { RUN_STATUS_LABELS, RUN_STATUS_TONES, type RunStatus } from "../../../lib/agent-contracts";

// Every console page distinguishes "you may not see this" from "the platform is not
// answering". An expired session always returns to sign-in instead of showing an error.
export function ConsoleError({ error, area, back, backLabel }: {
  error: unknown; area: string; back: string; backLabel: string;
}) {
  if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
  const denied = error instanceof ApiError && [403, 404].includes(error.status);
  return <section className="workspace-content evaluation-content">
    <h1>{denied ? `${area} not found or access denied` : `${area} unavailable`}</h1>
    <p role="alert">{denied
      ? "Check your workspace access and the requested resource."
      : "The platform could not be reached. Try again shortly."}</p>
    <a href={back}>{backLabel}</a>
  </section>;
}

export function InvalidLink({ back, backLabel }: { back: string; backLabel: string }) {
  return <section className="workspace-content evaluation-content"><h1>Invalid link</h1>
    <p role="alert">The requested identifier, filter or page cursor is invalid.</p>
    <a href={back}>{backLabel}</a>
  </section>;
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return <p className={"state " + RUN_STATUS_TONES[status]}>{RUN_STATUS_LABELS[status]}</p>;
}

export function Saved({ message }: { message: string | null }) {
  return message ? <p role="status">{message}</p> : null;
}

export function Failed({ message }: { message: string | null }) {
  return message ? <p role="alert">{message}</p> : null;
}
