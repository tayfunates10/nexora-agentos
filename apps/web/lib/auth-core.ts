import { randomBytes, createHash, timingSafeEqual } from "node:crypto";
import * as oidc from "openid-client";
import { z } from "zod";

export interface Store {
  get(key: string): Promise<string | null>;
  take(key: string): Promise<string | null>;
  put(key: string, value: string, ttl: number): Promise<void>;
  remove(key: string): Promise<void>;
}
export interface AuthConfig {
  origin: string; issuer: string; clientId: string; clientSecret: string;
  audience: string; apiUrl: string; redisUrl: string;
}
export function readConfig(env: Record<string, string | undefined> = process.env): AuthConfig | null {
  const values = [env.NEXORA_WEB_ORIGIN, env.NEXORA_AUTH_ISSUER, env.NEXORA_OIDC_CLIENT_ID,
    env.NEXORA_OIDC_CLIENT_SECRET, env.NEXORA_AUTH_AUDIENCE, env.NEXORA_SESSION_REDIS_URL];
  if (values.some(value => !value)) return null;
  const origin = new URL(values[0]!);
  const issuer = new URL(values[1]!);
  for (const url of [origin, issuer]) {
    if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname))) throw new Error("HTTPS required");
    if (url.username || url.password || url.search || url.hash) throw new Error("Invalid auth URL");
  }
  if (origin.pathname !== "/") throw new Error("Origin must not include a path");
  return { origin: origin.origin, issuer: issuer.href, clientId: values[2]!, clientSecret: values[3]!,
    audience: values[4]!, redisUrl: values[5]!, apiUrl: env.NEXORA_API_URL ?? "http://127.0.0.1:8000" };
}
export const opaqueId = () => randomBytes(32).toString("base64url");
const validId = (id: string) => /^[A-Za-z0-9_-]{43}$/.test(id);
export const sessionKey = (id: string) => "nexora:web:session:" + createHash("sha256").update(id).digest("hex");
const flowKey = (id: string) => "nexora:web:flow:" + createHash("sha256").update(id).digest("hex");
export const sessionSchema = z.object({ accessToken: z.string(), subject: z.string(), issuer: z.string(), csrf: z.string(), expiresAt: z.number() });
export type Session = z.infer<typeof sessionSchema>;
const flowSchema = z.object({ verifier: z.string(), state: z.string(), nonce: z.string() });
export function cookieNames(config: AuthConfig) {
  const prefix = config.origin.startsWith("https:") ? "__Host-" : "";
  return { session: prefix + "nexora_session", flow: prefix + "nexora_flow" };
}
export function cookieOptions(config: AuthConfig, maxAge: number) {
  return { httpOnly: true, secure: config.origin.startsWith("https:"), sameSite: "lax" as const, path: "/", maxAge };
}
export function validRequestSource(origin: string | null, referer: string | null, config: AuthConfig) {
  if (origin) return origin === config.origin;
  if (!referer) return false;
  try { return new URL(referer).origin === config.origin; } catch { return false; }
}
export function validMutation(origin: string | null, config: AuthConfig, supplied: string, expected: string, referer: string | null = null) {
  if (!validRequestSource(origin, referer, config) || !supplied || supplied.length !== expected.length) return false;
  const a = Buffer.from(supplied); const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
export async function configuration(config: AuthConfig) {
  return oidc.discovery(new URL(config.issuer), config.clientId, config.clientSecret, undefined, {
    timeout: 5,
    execute: [oidc.enableNonRepudiationChecks, ...(config.issuer.startsWith("http:") ? [oidc.allowInsecureRequests] : [])],
  });
}
export async function beginLogin(config: AuthConfig, store: Store) {
  const provider = await configuration(config);
  const flow = { verifier: oidc.randomPKCECodeVerifier(), state: oidc.randomState(), nonce: oidc.randomNonce() };
  const id = opaqueId();
  await store.put(flowKey(id), JSON.stringify(flow), 600);
  const url = oidc.buildAuthorizationUrl(provider, { redirect_uri: config.origin + "/auth/callback",
    scope: "openid profile", audience: config.audience, state: flow.state, nonce: flow.nonce,
    code_challenge: await oidc.calculatePKCECodeChallenge(flow.verifier), code_challenge_method: "S256" });
  return { id, url };
}
export async function finishLogin(config: AuthConfig, store: Store, flowId: string, callback: URL) {
  if (!validId(flowId) || callback.origin !== config.origin || callback.pathname !== "/auth/callback") throw new Error("Invalid callback");
  const raw = await store.take(flowKey(flowId)); // atomic one-use transaction
  if (!raw) throw new Error("Expired login");
  const flow = flowSchema.parse(JSON.parse(raw));
  const provider = await configuration(config);
  const tokens = await oidc.authorizationCodeGrant(provider, callback, {
    pkceCodeVerifier: flow.verifier, expectedState: flow.state, expectedNonce: flow.nonce, idTokenExpected: true,
  });
  const claims = tokens.claims();
  if (!claims || tokens.token_type.toLowerCase() !== "bearer") throw new Error("Invalid identity");
  // The API independently validates access-token signature, issuer and API audience.
  const me = await fetch(new URL("/api/v1/me", config.apiUrl), { headers: { Authorization: "Bearer " + tokens.access_token },
    cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000) });
  if (!me.ok) throw new Error("Access token rejected");
  const identity = z.object({ issuer: z.string(), subject: z.string() }).parse(await me.json());
  if (identity.subject !== claims.sub || identity.issuer !== claims.iss) throw new Error("Identity mismatch");
  const seconds = tokens.expiresIn();
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 1) throw new Error("Missing token lifetime");
  const ttl = Math.min(3600, Math.floor(seconds));
  const id = opaqueId();
  const session: Session = { accessToken: tokens.access_token, subject: identity.subject, issuer: identity.issuer,
    csrf: opaqueId(), expiresAt: Date.now() + ttl * 1000 };
  await store.put(sessionKey(id), JSON.stringify(session), ttl);
  return { id, ttl };
}
export async function loadSession(store: Store, id: string | undefined): Promise<Session | null> {
  if (!id || !validId(id)) return null;
  const raw = await store.get(sessionKey(id));
  if (!raw) return null;
  try {
    const session = sessionSchema.parse(JSON.parse(raw));
    if (session.expiresAt <= Date.now()) { await store.remove(sessionKey(id)); return null; }
    return session;
  } catch { return null; }
}
export async function endSession(store: Store, id: string | undefined) {
  if (id && validId(id)) await store.remove(sessionKey(id));
}
