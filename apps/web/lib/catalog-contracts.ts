import { z } from "zod";
import { integrationStatusSchema } from "./integration-contracts.ts";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

const timestamp = z.iso.datetime({ offset: true });
const slug = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(2).max(64);
const semver = z.string().regex(/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/);
const action = z.string().max(100);

export const agentStatusSchema = z.enum(["draft", "beta", "stable", "deprecated", "disabled"]);
export const channelSchema = z.enum(["stable", "beta", "canary"]);
export const updateModeSchema = z.enum(["manual", "automatic"]);
export const instanceStatusSchema = z.enum(["active", "paused", "disabled"]);
export type InstanceStatus = z.infer<typeof instanceStatusSchema>;
export type Channel = z.infer<typeof channelSchema>;

// The catalog entry is the whole contract the console needs to render an agent. No screen
// knows the name of any particular agent: adding one is a published manifest, not a build.
export const catalogEntrySchema = z.object({
  catalog_agent_id: z.uuid(),
  slug,
  name: z.string().min(1).max(100),
  description: z.string().min(1).max(1000),
  category: z.string().min(1).max(40),
  icon: z.string().min(1).max(40),
  status: agentStatusSchema,
  available_version: semver.nullable(),
  installed_count: z.number().int().nonnegative(),
  required_integrations: z.array(slug),
  optional_integrations: z.array(slug),
  capabilities: z.array(action),
  approval_required: z.array(action),
});
export type CatalogEntry = z.infer<typeof catalogEntrySchema>;

export const catalogPageSchema = z.object({
  items: z.array(catalogEntrySchema), next_cursor: z.string().nullable(),
});

export const requirementSchema = z.object({
  integration_definition_id: slug,
  name: z.string().min(1).max(100),
  required: z.boolean(),
  bound_integration_id: z.uuid().nullable(),
  bound_display_name: z.string().max(100).nullable(),
  bound_status: integrationStatusSchema.nullable(),
  satisfied: z.boolean(),
});
export type Requirement = z.infer<typeof requirementSchema>;

export const readinessSchema = z.object({
  ready: z.boolean(),
  requirements: z.array(requirementSchema),
  missing_required: z.array(slug),
}).refine(
  // The API states readiness and the reason for it; the console refuses a payload that
  // claims to be ready while naming something missing.
  value => value.ready === (value.missing_required.length === 0),
  "Readiness must agree with the missing requirements it lists",
);

export const bindingSchema = z.object({
  binding_key: slug,
  tenant_integration_id: z.uuid(),
  display_name: z.string().min(1).max(100),
  status: integrationStatusSchema,
  created_at: timestamp,
  updated_at: timestamp,
});

const manifestSchema = z.object({
  id: z.string().max(100),
  slug,
  name: z.string().min(1).max(100),
  description: z.string().min(1).max(1000),
  version: semver,
  min_runtime_version: semver,
  system_instructions: z.string().min(1).max(20000),
  capabilities: z.array(action),
  approval_required: z.array(action),
  required_integrations: z.array(slug),
  optional_integrations: z.array(slug),
  changelog: z.string().max(5000).default(""),
  settings_schema: z.record(z.string(), z.unknown()).default({}),
}).loose();
export type AgentManifest = z.infer<typeof manifestSchema>;

export const tenantAgentSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  catalog_agent_id: z.uuid(),
  slug,
  display_name: z.string().min(1).max(100),
  version: semver,
  available_version: semver.nullable(),
  status: instanceStatusSchema,
  update_channel: channelSchema,
  update_mode: updateModeSchema,
  instructions_override: z.string().max(20000).nullable(),
  settings: z.record(z.string(), z.unknown()),
  manifest: manifestSchema,
  bindings: z.array(bindingSchema),
  readiness: readinessSchema,
  created_at: timestamp,
  updated_at: timestamp,
});
export type TenantAgent = z.infer<typeof tenantAgentSchema>;

export const tenantAgentSummarySchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  catalog_agent_id: z.uuid(),
  slug,
  display_name: z.string().min(1).max(100),
  version: semver,
  available_version: semver.nullable(),
  status: instanceStatusSchema,
  update_channel: channelSchema,
  update_mode: updateModeSchema,
  ready: z.boolean(),
  created_at: timestamp,
  updated_at: timestamp,
});
export type TenantAgentSummary = z.infer<typeof tenantAgentSummarySchema>;

export const tenantAgentPageSchema = z.object({
  items: z.array(tenantAgentSummarySchema), next_cursor: z.uuid().nullable(),
});

export const versionEventSchema = z.object({
  id: z.uuid(),
  action: z.enum(["installed", "updated", "rolled_back"]),
  from_version: semver.nullable(),
  to_version: semver,
  actor_subject: z.string().min(1).max(255),
  created_at: timestamp,
});
export const versionEventPageSchema = z.object({
  items: z.array(versionEventSchema), next_cursor: z.uuid().nullable(),
});

export const updatePolicySchema = z.object({
  workspace_id: z.uuid(),
  channel: channelSchema,
  mode: updateModeSchema,
  updated_at: timestamp,
});

export const forkedAgentSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  name: z.string().min(1).max(100),
  instructions: z.string().min(1).max(20000),
  model_profile: z.string().min(1).max(64),
  origin_slug: slug,
  origin_version: semver,
  created_at: timestamp,
});

export const INSTANCE_STATUS_TONES: Record<InstanceStatus, Tone> = {
  active: "up",
  paused: "warn",
  disabled: "neutral",
};

export function instanceStatusKey(status: InstanceStatus): MessageKey {
  return `catalog.instanceStatus.${status}`;
}

/** Compare two semantic versions numerically, so 1.10.0 outranks 1.9.0. */
export function compareVersions(left: string, right: string): number {
  const [a, b] = [left, right].map(value => value.split(".").map(Number));
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] > b[index] ? 1 : -1;
  }
  return 0;
}

/** True when the catalog offers this instance something newer than it runs. */
export function updateAvailable(agent: { version: string; available_version: string | null }): boolean {
  return agent.available_version !== null
    && compareVersions(agent.available_version, agent.version) > 0;
}

/** The connections an instance still needs before it can be switched on. */
export function unmetRequirements(agent: TenantAgent): Requirement[] {
  return agent.readiness.requirements.filter(item => item.required && !item.satisfied);
}
