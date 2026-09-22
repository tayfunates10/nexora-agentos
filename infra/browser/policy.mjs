import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

export function publicIPv4(address) {
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

export function publicIPv6(address) {
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

function assertPublicAddress(address, family) {
  const valid = family === 4 ? publicIPv4(address) : family === 6 ? publicIPv6(address) : false;
  if (!valid) throw new Error("host_not_routable");
}

export async function resolvePublicHost(hostname, resolver = lookup) {
  const literal = isIP(hostname);
  if (literal) {
    assertPublicAddress(hostname, literal);
    return [hostname];
  }
  if (hostname.toLowerCase() === "localhost") throw new Error("host_not_routable");
  const addresses = await resolver(hostname, { all: true, verbatim: true });
  if (!addresses.length) throw new Error("host_unresolvable");
  for (const entry of addresses) {
    assertPublicAddress(entry.address, entry.family);
  }
  return [...new Set(addresses.map(entry => entry.address))];
}

export function hostResolverRule(hostname, address) {
  if (isIP(hostname)) return null;
  const family = isIP(address);
  if (!family) throw new Error("host_unresolvable");
  const mapped = family === 6 ? `[${address}]` : address;
  return `MAP ${hostname} ${mapped}, EXCLUDE localhost`;
}

export function target(value, allowedOrigin) {
  const url = new URL(value);
  const allowed = new URL(allowedOrigin);
  if (
    url.protocol !== "https:" || allowed.protocol !== "https:" ||
    url.username || url.password || allowed.username || allowed.password ||
    url.origin !== allowed.origin
  ) throw new Error("origin_not_allowed");
  return url;
}
