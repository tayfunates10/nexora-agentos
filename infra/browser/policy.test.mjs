import assert from "node:assert/strict";
import test from "node:test";
import { hostResolverRule, resolvePublicHost, target } from "./policy.mjs";

test("resolvePublicHost rejects private and mixed DNS answers", async () => {
  await assert.rejects(
    resolvePublicHost("example.test", async () => [{ address: "10.0.0.5", family: 4 }]),
    /host_not_routable/,
  );
  await assert.rejects(
    resolvePublicHost("example.test", async () => [
      { address: "93.184.216.34", family: 4 },
      { address: "127.0.0.1", family: 4 },
    ]),
    /host_not_routable/,
  );
});

test("resolvePublicHost normalizes IPv6 literals and rejects reserved ranges", async () => {
  assert.deepEqual(
    await resolvePublicHost("[2606:4700:4700::1111]"),
    ["2606:4700:4700::1111"],
  );
  await assert.rejects(resolvePublicHost("[::1]"), /host_not_routable/);
  await assert.rejects(resolvePublicHost("[2001:db8::1]"), /host_not_routable/);
});

test("resolvePublicHost rejects reserved IPv4 documentation and benchmark ranges", async () => {
  for (const address of ["192.0.2.1", "198.18.0.1", "198.51.100.10", "203.0.113.7"]) {
    await assert.rejects(resolvePublicHost(address), /host_not_routable/);
  }
});

test("resolvePublicHost returns deduplicated public addresses", async () => {
  const addresses = await resolvePublicHost("example.test", async () => [
    { address: "93.184.216.34", family: 4 },
    { address: "93.184.216.34", family: 4 },
    { address: "2606:2800:220:1:248:1893:25c8:1946", family: 6 },
  ]);
  assert.deepEqual(addresses, [
    "93.184.216.34",
    "2606:2800:220:1:248:1893:25c8:1946",
  ]);
});

test("hostResolverRule pins the validated address without changing the URL hostname", () => {
  assert.equal(
    hostResolverRule("example.test", "93.184.216.34"),
    "MAP example.test 93.184.216.34, EXCLUDE localhost",
  );
  assert.equal(
    hostResolverRule("example.test", "2606:2800:220:1:248:1893:25c8:1946"),
    "MAP example.test [2606:2800:220:1:248:1893:25c8:1946], EXCLUDE localhost",
  );
  assert.equal(hostResolverRule("93.184.216.34", "93.184.216.34"), null);
});

test("target requires exact HTTPS origin", () => {
  assert.equal(
    target("https://example.test/path", "https://example.test").href,
    "https://example.test/path",
  );
  assert.throws(() => target("https://evil.test/", "https://example.test"), /origin_not_allowed/);
  assert.throws(() => target("http://example.test/", "https://example.test"), /origin_not_allowed/);
});
