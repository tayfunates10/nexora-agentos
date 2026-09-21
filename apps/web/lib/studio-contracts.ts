import { z } from "zod";
import { agentStatusSchema, channelSchema } from "./catalog-contracts.ts";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

const timestamp = z.iso.datetime({ offset: true });
const slug = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(3).max(64);
const semver = z.string().regex(/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/);
const label = z.string().regex(/^[a-z][a-z0-9_-]{1,39}$/);

export const visibilitySchema = z.enum(["restricted", "public"]);
export const rolloutStateSchema = z.enum(["active", "paused", "completed", "rolled_back"]);
export type RolloutState = z.infer<typeof rolloutStateSchema>;

export const catalogAgentSchema = z.object({
  id: z.uuid(),
  slug,
  name: z.string().min(1).max(100),
  description: z.string().min(1).max(1000),
  category: label,
  icon: label,
  status: agentStatusSchema,
  visibility: visibilitySchema,
  latest_version: semver.nullable(),
  version_count: z.number().int().nonnegative(),
  created_at: timestamp,
  updated_at: timestamp,
});
export type CatalogAgent = z.infer<typeof catalogAgentSchema>;

export const catalogAgentPageSchema = z.object({
  items: z.array(catalogAgentSchema), next_cursor: z.string().nullable(),
});

export const catalogVersionSchema = z.object({
  id: z.uuid(),
  version: semver,
  status: agentStatusSchema,
  channel: channelSchema,
  min_runtime_version: semver,
  changelog: z.string().max(5000),
  created_at: timestamp,
});
export const catalogVersionPageSchema = z.object({
  items: z.array(catalogVersionSchema), next_cursor: semver.nullable(),
});

export const rolloutSchema = z.object({
  id: z.uuid(),
  catalog_agent_id: z.uuid(),
  version_id: z.uuid(),
  version: semver,
  channel: channelSchema,
  percentage: z.number().int().min(0).max(100),
  state: rolloutStateSchema,
  created_at: timestamp,
  updated_at: timestamp,
});
export const rolloutPageSchema = z.object({
  items: z.array(rolloutSchema), next_cursor: z.uuid().nullable(),
});

export const catalogAgentInput = z.object({
  slug,
  name: z.string().trim().min(1).max(100),
  description: z.string().trim().min(1).max(1000),
  category: label,
  icon: label,
  status: agentStatusSchema,
  visibility: visibilitySchema,
});

export const rolloutInput = z.object({
  version: semver,
  channel: channelSchema,
  percentage: z.coerce.number().int().min(0).max(100),
});

export const ROLLOUT_STATE_TONES: Record<RolloutState, Tone> = {
  active: "up",
  paused: "warn",
  completed: "neutral",
  rolled_back: "down",
};

export function rolloutStateKey(state: RolloutState): MessageKey {
  return `studio.rolloutState.${state}`;
}

/**
 * A manifest a platform administrator pasted. It is bounded and parsed here so an
 * unreadable document is reported on the form rather than as a failed API call, but it is
 * forwarded whole: the platform's own contract decides whether it may be published.
 */
export function parseManifest(raw: string, slugExpected: string): Record<string, unknown> {
  if (raw.length > 200_000) throw new Error("Manifest is too large");
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("A manifest must be a JSON object");
  }
  const document = parsed as Record<string, unknown>;
  if (document.slug !== slugExpected) {
    throw new Error("The manifest belongs to a different catalog agent");
  }
  if (typeof document.version !== "string") throw new Error("A manifest must state a version");
  return document;
}

/** The staged percentages an administrator moves a rollout through. */
export const ROLLOUT_STEPS = [5, 25, 50, 100] as const;

/** The next step up from where a rollout is now, or null once it has reached everyone. */
export function nextStep(percentage: number): number | null {
  return ROLLOUT_STEPS.find(step => step > percentage) ?? null;
}
