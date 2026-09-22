import { createServer } from "node:http";
import { lookup } from "node:dns/promises";
import { isIP } from "node:net";
import { timingSafeEqual } from "node:crypto";
import { chromium } from "@playwright/test";

const PORT = Number(process.env.PORT ?? "8080");
const TOKEN = process.env.NEXORA_BROWSER_RUNTIME_TOKEN ?? "";
const MAX_BODY = 16_384;
const MAX_TEXT = 48_000;
const MAX_LINKS = 100;

if (TOKEN.length < 32) {
  throw new Error("NEXORA_BROWSER_RUNTIME_TOKEN must be at least 32 characters");
}

function authorized(value) {
  const expected = Buffer.from("Bearer " + TOKEN);
  const actual = Buffer.from(value ?? "");
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

function publicIPv4(address) {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255)) return false;
  const [a, b] = parts;
  if (
    a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168)
  ) return false;
  return true;
}

function publicIPv6(address) {
  const value = address.toLowerCase();
  if (
    value === "::" || value === "::1" || value.startsWith("fc") || value.startsWith("fd") ||
    value.startsWith("fe8") || value.startsWith("fe9") || value.startsWith("fea") ||
    value.startsWith("feb")
  ) return false;
  if (value.startsWith("::ffff:")) {
    const mapped = value.slice(7);
    return isIP(mapped) === 4 ? publicIPv4(mapped) : false;
  }
  return true;
}

async function assertPublicHost(hostname) {
  const literal = isIP(hostname);
  if (literal === 4 && !publicIPv4(hostname)) throw new Error("host_not_routable");
  if (literal === 6 && !publicIPv6(hostname)) throw new Error("host_not_routable");
  if (literal) return;
  if (hostname.toLowerCase() === "localhost") throw new Error("host_not_routable");
  const addresses = await lookup(hostname, { all: true, verbatim: true });
  if (!addresses.length) throw new Error("host_unresolvable");
  for (const entry of addresses) {
    if (entry.family === 4 ? !publicIPv4(entry.address) : !publicIPv6(entry.address)) {
      throw new Error("host_not_routable");
    }
  }
}

function target(value, allowedOrigin) {
  const url = new URL(value);
  const allowed = new URL(allowedOrigin);
  if (
    url.protocol !== "https:" || allowed.protocol !== "https:" ||
    url.username || url.password || allowed.username || allowed.password ||
    url.origin !== allowed.origin
  ) throw new Error("origin_not_allowed");
  return url;
}

async function bodyOf(request) {
  let raw = "";
  for await (const chunk of request) {
    raw += chunk;
    if (Buffer.byteLength(raw) > MAX_BODY) throw new Error("request_too_large");
  }
  return JSON.parse(raw || "{}");
}

const browser = await chromium.launch({ headless: true });

const server = createServer(async (request, response) => {
  const send = (status, payload) => {
    response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(JSON.stringify(payload));
  };
  try {
    if (request.method === "GET" && request.url === "/health/live") {
      return send(200, { ok: true });
    }
    if (request.method !== "POST" || request.url !== "/inspect") return send(404, { error: "not_found" });
    if (!authorized(request.headers.authorization)) return send(401, { error: "unauthorized" });

    const input = await bodyOf(request);
    if (typeof input.url !== "string" || typeof input.allowed_origin !== "string") {
      return send(422, { error: "invalid_request" });
    }
    const url = target(input.url, input.allowed_origin);
    await assertPublicHost(url.hostname);

    const context = await browser.newContext({
      acceptDownloads: false,
      serviceWorkers: "block",
      ignoreHTTPSErrors: false,
    });
    try {
      const page = await context.newPage();
      page.setDefaultTimeout(8_000);
      await page.route("**/*", async route => {
        try {
          const requestUrl = new URL(route.request().url());
          if (requestUrl.protocol !== "https:" || requestUrl.origin !== url.origin) {
            return route.abort("blockedbyclient");
          }
          return route.continue();
        } catch {
          return route.abort("blockedbyclient");
        }
      });
      page.on("dialog", dialog => void dialog.dismiss());
      const result = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 12_000 });
      if (!result) return send(502, { error: "navigation_failed" });
      const finalUrl = new URL(page.url());
      if (finalUrl.origin !== url.origin) return send(403, { error: "redirect_blocked" });

      const title = (await page.title()).slice(0, 500);
      const text = (await page.locator("body").innerText()).slice(0, MAX_TEXT);
      const links = await page.locator("a[href]").evaluateAll((nodes, limit) =>
        nodes.slice(0, limit).map(node => ({
          text: (node.textContent ?? "").trim().slice(0, 300),
          href: node.getAttribute("href") ?? "",
        })), MAX_LINKS
      );
      return send(200, {
        ok: true,
        url: finalUrl.href,
        status_code: result.status(),
        title,
        text,
        links,
        truncated: text.length >= MAX_TEXT,
      });
    } finally {
      await context.close();
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : "browser_error";
    const status = ["origin_not_allowed", "host_not_routable"].includes(code) ? 403 : 502;
    return send(status, { error: code });
  }
});

server.listen(PORT, "0.0.0.0");

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, async () => {
    server.close();
    await browser.close();
    process.exit(0);
  });
}
