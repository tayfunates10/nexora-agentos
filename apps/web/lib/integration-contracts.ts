import { z } from "zod";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

const timestamp = z.iso.datetime({ offset: true });
const slug = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(2).max(64);

export const authTypeSchema = z.enum(["api_key", "oauth2", "basic", "bearer_token", "webhook"]);
export type AuthType = z.infer<typeof authTypeSchema>;

export const integrationStatusSchema = z.enum([
  "pending", "connected", "expired", "revoked", "error", "disabled",
]);
export type IntegrationStatus = z.infer<typeof integrationStatusSchema>;

export const credentialFieldSchema = z.object({
  key: z.string().min(1).max(64),
  label: z.string().min(1).max(100),
  secret: z.boolean(),
  required: z.boolean(),
  help: z.string().max(300).default(""),
  pattern: z.string().max(200).nullable().default(null),
});
export type CredentialField = z.infer<typeof credentialFieldSchema>;

export const definitionSchema = z.object({
  id: slug,
  name: z.string().min(1).max(100),
  description: z.string().min(1).max(1000),
  category: z.string().min(1).max(40),
  icon: z.string().min(1).max(40),
  auth_type: authTypeSchema,
  status: z.enum(["available", "beta", "deprecated", "disabled"]),
  version: z.string().min(5).max(32),
  capabilities: z.array(z.string().max(100)),
  scopes: z.array(z.string().max(200)),
  credential_fields: z.array(credentialFieldSchema),
});
export type IntegrationDefinition = z.infer<typeof definitionSchema>;

export const definitionPageSchema = z.object({
  items: z.array(definitionSchema), next_cursor: z.string().nullable(),
});

// A credential hint is the only representation of a secret the console ever receives. The
// schema states that: a value that looks like a real secret is a contract violation.
const credentialHint = z.string().min(1).max(64).refine(
  hint => hint.startsWith("•"),
  "A credential is only ever shown as a masked hint",
);

export const integrationSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  integration_definition_id: slug,
  display_name: z.string().min(1).max(100),
  account_identifier: z.string().min(1).max(200),
  auth_type: authTypeSchema,
  status: integrationStatusSchema,
  scopes: z.array(z.string().max(200)),
  granted_scopes: z.array(z.string().max(200)),
  config: z.record(z.string(), z.string()),
  credential_hint: credentialHint.nullable(),
  credential_expires_at: timestamp.nullable(),
  created_at: timestamp,
  updated_at: timestamp,
  last_tested_at: timestamp.nullable(),
  last_success_at: timestamp.nullable(),
  last_error: z.string().max(500).nullable(),
});
export type TenantIntegration = z.infer<typeof integrationSchema>;

export const integrationPageSchema = z.object({
  items: z.array(integrationSchema), next_cursor: z.uuid().nullable(),
});

export const connectionTestSchema = z.object({
  integration_id: z.uuid(),
  status: integrationStatusSchema,
  ok: z.boolean(),
  checked_at: timestamp,
  granted_scopes: z.array(z.string().max(200)),
  missing_scopes: z.array(z.string().max(200)),
  error_code: z.string().max(64).nullable(),
});

export const connectInput = z.object({
  integration_definition_id: slug,
  display_name: z.string().trim().min(1).max(100),
  account_identifier: z.string().trim().min(1).max(200),
  credentials: z.record(z.string().max(64), z.string().max(8192)),
});

// Colour is an accent on a state that is always written out in words next to it.
export const INTEGRATION_STATUS_TONES: Record<IntegrationStatus, Tone> = {
  pending: "warn",
  connected: "up",
  expired: "warn",
  revoked: "down",
  error: "down",
  disabled: "neutral",
};

export function integrationStatusKey(status: IntegrationStatus): MessageKey {
  return `integrations.status.${status}`;
}

/** A connection the tenant must act on before an agent bound to it can run. */
export function needsAttention(integration: TenantIntegration): boolean {
  return integration.status === "expired" || integration.status === "error"
    || integration.status === "revoked";
}

/**
 * The fields a connector asks a tenant to fill in, in a stable order: identifying
 * details first, secrets last, so a form reads the way a person fills it.
 */
export function orderedFields(definition: IntegrationDefinition): CredentialField[] {
  return [...definition.credential_fields].sort(
    (left, right) => Number(left.secret) - Number(right.secret),
  );
}

/** Scopes the provider granted that the connector asked for, and those it withheld. */
export function scopeReport(integration: TenantIntegration): {
  granted: string[]; missing: string[];
} {
  const granted = integration.granted_scopes.length
    ? integration.granted_scopes
    : integration.scopes;
  return {
    granted,
    missing: integration.scopes.filter(scope => !granted.includes(scope)),
  };
}
