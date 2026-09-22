import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { chromium } from "@playwright/test";
import { hostResolverRule, resolvePublicHost, target } from "./policy.mjs";

const PORT = Number(process.env.PORT ?? "8080");
const TOKEN = process.env.NEXORA_BROWSER_RUNTIME_TOKEN ?? "";
const MAX_BODY = 16_384;
const MAX_TEXT = 48_000;
const MAX_LINKS = 100;
const MAX_SELECTOR = 500;
const MAX_FILL = 4_000;

if (TOKEN.length < 32) {
  throw new Error("NEXORA_BROWSER_RUNTIME_TOKEN must be at least 32 characters");
}

function authorized(value) {
  const expected = Buffer.from("Bearer " + TOKEN);
  const actual = Buffer.from(value ?? "");
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

async function bodyOf(request) {
  let raw = "";
  for await (const chunk of request) {
    raw += chunk;
    if (Buffer.byteLength(raw) > MAX_BODY) throw new Error("request_too_large");
  }
  return JSON.parse(raw || "{}");
}

async function evidence(page, initialResponse, action = null) {
  const finalUrl = new URL(page.url());
  const title = (await page.title()).slice(0, 500);
  const bodyText = await page.locator("body").innerText();
  const text = bodyText.slice(0, MAX_TEXT);
  const links = await page.locator("a[href]").evaluateAll((nodes, limit) =>
    nodes.slice(0, limit).map(node => ({
      text: (node.textContent ?? "").trim().slice(0, 300),
      href: node.getAttribute("href") ?? "",
    })), MAX_LINKS
  );
  return {
    ok: true,
    url: finalUrl.href,
    status_code: initialResponse.status(),
    title,
    text,
    links,
    truncated: bodyText.length > MAX_TEXT,
    ...(action ? { action } : {}),
  };
}

const activeBrowsers = new Set();

async function isolatedPage(url, pinnedAddress) {
  const resolverRule = hostResolverRule(url.hostname, pinnedAddress);
  const browser = await chromium.launch({
    headless: true,
    ...(resolverRule ? { args: [`--host-resolver-rules=${resolverRule}`] } : {}),
  });
  activeBrowsers.add(browser);
  const context = await browser.newContext({
    acceptDownloads: false,
    serviceWorkers: "block",
    ignoreHTTPSErrors: false,
  });
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
  return { browser, context, page };
}

const server = createServer(async (request, response) => {
  const send = (status, payload) => {
    response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(JSON.stringify(payload));
  };
  try {
    if (request.method === "GET" && request.url === "/health/live") {
      return send(200, { ok: true });
    }
    if (request.method !== "POST" || !["/inspect", "/action"].includes(request.url ?? "")) {
      return send(404, { error: "not_found" });
    }
    if (!authorized(request.headers.authorization)) return send(401, { error: "unauthorized" });

    const input = await bodyOf(request);
    if (typeof input.url !== "string" || typeof input.allowed_origin !== "string") {
      return send(422, { error: "invalid_request" });
    }
    const url = target(input.url, input.allowed_origin);
    const publicAddresses = await resolvePublicHost(url.hostname);
    const { browser, context, page } = await isolatedPage(url, publicAddresses[0]);
    try {
      const result = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 12_000 });
      if (!result) return send(502, { error: "navigation_failed" });
      if (new URL(page.url()).origin !== url.origin) {
        return send(403, { error: "redirect_blocked" });
      }

      if (request.url === "/inspect") {
        return send(200, await evidence(page, result));
      }

      const action = input.action;
      const selector = input.selector;
      if (
        !["click", "fill"].includes(action) ||
        typeof selector !== "string" || selector.length < 1 || selector.length > MAX_SELECTOR
      ) {
        return send(422, { error: "invalid_action" });
      }
      if (action === "fill" && (typeof input.value !== "string" || input.value.length > MAX_FILL)) {
        return send(422, { error: "invalid_action_value" });
      }

      const locator = page.locator(selector);
      const count = await locator.count();
      if (count !== 1) return send(422, { error: "ambiguous_selector" });

      if (action === "fill") {
        const sensitive = await locator.evaluate(node => {
          if (!(node instanceof HTMLInputElement)) return false;
          const type = (node.type || "").toLowerCase();
          const autocomplete = (node.autocomplete || "").toLowerCase();
          return type === "password" ||
            ["current-password", "new-password", "one-time-code"].includes(autocomplete);
        });
        if (sensitive) return send(403, { error: "sensitive_field_blocked" });
        await locator.fill(input.value);
      } else {
        await locator.click();
      }

      await page.waitForLoadState("domcontentloaded", { timeout: 3_000 }).catch(() => {});
      if (new URL(page.url()).origin !== url.origin) {
        return send(403, { error: "navigation_blocked" });
      }
      return send(200, await evidence(page, result, action));
    } finally {
      // A fresh browser process pins the already-validated DNS result, closing the
      // validation-to-connect rebinding window. Context state cannot cross requests.
      await context.close();
      activeBrowsers.delete(browser);
      await browser.close();
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : "browser_error";
    const status = ["origin_not_allowed", "host_not_routable"].includes(code) ? 403 : 502;
    return send(status, { error: code });
  }
});

server.listen(PORT, "0.0.0.0");

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    server.close(async () => {
      await Promise.all(
        [...activeBrowsers].map(browser => browser.close().catch(() => {})),
      );
      process.exit(0);
    });
  });
}
