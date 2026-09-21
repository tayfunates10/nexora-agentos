import { fetchHealth, type Health } from "../lib/health";
import { PublicShell } from "../components/shell/PublicShell.tsx";
import { Reveal } from "../components/ui/Reveal.tsx";
import {
  Card, Notice, SectionHead, StatusBadge, RefreshLink,
} from "../components/ui/primitives.tsx";
import { Icon } from "../components/ui/Icon.tsx";
import { getTheme, getUi } from "../lib/i18n/server.ts";
import type { MessageKey } from "../messages/en.ts";
import type { Tone } from "../lib/i18n/tone.ts";

export const dynamic = "force-dynamic";

// What the signed-in console actually offers. Each entry is a real page behind sign-in;
// this landing page never claims a capability the platform does not have.
const CAPABILITIES = [
  "workspaces", "runs", "tools", "knowledge", "evaluations", "spend",
] as const;

type ServiceState = "up" | "down" | "unknown";

const SERVICE_TONES: Record<ServiceState, Tone> = { up: "up", down: "down", unknown: "warn" };

function services(health: Health | null): { key: string; state: ServiceState }[] {
  return [
    { key: "api", state: health ? "up" : "unknown" },
    { key: "postgres", state: health?.dependencies.postgres ?? "unknown" },
    { key: "redis", state: health?.dependencies.redis ?? "unknown" },
  ];
}

export default async function Home() {
  const [ui, theme] = await Promise.all([getUi(), getTheme()]);
  const state = await fetchHealth(process.env.NEXORA_API_URL ?? "http://127.0.0.1:8000");
  const health = state.kind === "connected" ? state.health : null;

  // Every reason the health view can be incomplete is reported as itself: a restricted
  // read, an unreachable API and a contradictory payload are not the same thing.
  const noticeKey: MessageKey = state.kind === "forbidden" ? "health.notice.forbidden"
    : state.kind === "invalid" ? "health.notice.invalid"
    : state.kind === "unavailable" ? "health.notice.unavailable"
    : health?.status === "degraded" ? "health.notice.degraded"
    : "health.notice.allUp";
  const healthy = noticeKey === "health.notice.allUp";

  return <PublicShell
    ui={ui}
    theme={theme}
    actions={<a className="button" href="/workspaces">
      {ui.t("landing.openWorkspaces")}<Icon name="arrowRight" size={18}/>
    </a>}
  >
    <section className="hero enter">
      <p className="eyebrow">{ui.t("landing.eyebrow")}</p>
      <h1>{ui.t("landing.titleLead")}<span>{ui.t("landing.titleAccent")}</span></h1>
      <p className="page-intro">{ui.t("landing.intro")}</p>
    </section>

    <Reveal>
      <section aria-labelledby="health-title" className="stack-lg">
        <SectionHead
          id="health-title"
          title={ui.t("health.title")}
          action={<RefreshLink href="/" label={ui.t("common.refreshStatus")}/>}
        />
        <Notice live="status" tone={healthy ? "success" : "warning"}>{ui.t(noticeKey)}</Notice>
        <div className="card-grid">
          {services(health).map(service => <Card key={service.key}>
            <StatusBadge tone={SERVICE_TONES[service.state]}>
              {ui.t(`health.status.${service.state}`)}
            </StatusBadge>
            <h3 className="section-title">{ui.t(`health.service.${service.key}.name` as MessageKey)}</h3>
            <p className="notice">{ui.t(`health.service.${service.key}.detail` as MessageKey)}</p>
          </Card>)}
        </div>
      </section>
    </Reveal>

    <Reveal>
      <section aria-labelledby="capability-title" className="stack-lg">
        <SectionHead
          id="capability-title"
          title={ui.t("landing.consoleTitle")}
          action={<a className="link" href="/workspaces">{ui.t("landing.signInToWorkspace")}</a>}
        />
        <div className="card-grid">
          {CAPABILITIES.map(capability => <Card key={capability}>
            <h3 className="section-title">
              {ui.t(`landing.capability.${capability}.title` as MessageKey)}</h3>
            <p className="notice">{ui.t(`landing.capability.${capability}.detail` as MessageKey)}</p>
          </Card>)}
        </div>
      </section>
    </Reveal>

    <Reveal>
      <section className="operator-panel" aria-labelledby="operator-title">
        <div>
          <p className="eyebrow">{ui.t("landing.operatorEyebrow")}</p>
          <h2 id="operator-title" className="section-title">{ui.t("landing.operatorTitle")}</h2>
          <p className="notice">{ui.t("landing.operatorDetail")}</p>
        </div>
        <a className="button secondary" href="/workspaces">
          {ui.t("landing.openWorkspaces")}<Icon name="arrowRight" size={18}/>
        </a>
      </section>
    </Reveal>
  </PublicShell>;
}
