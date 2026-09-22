import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

export function publicIPv4(address) {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255)) return false;
  const [a, b, c] = parts;
  if (
    a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 0 && c === 0) ||
    (a === 192 && b === 0 && c === 2) ||
    (a === 192 && b === 168) ||
    (a === 198 && (b === 18 || b === 19)) ||
    (a === 198 && b === 51 && c === 100) ||
    (a === 203 && b === 0 && c === 113)
  ) return false;
  return true;
}

export function publicIPv6(address) {
  const value = address.toLowerCase();
  if (
    value === "::" || value === "::1" || value.startsWith("fc") || value.startsWith("fd") ||
    value.startsWith("fe8") || value.startsWith("fe9") || value.startsWith("fea") ||
    value.startsWith("feb") || value.startsWith("fec") || value.startsWith("fed") ||
    value.startsWith("fee") || value.startsWith("fef") || value.startsWith("ff") ||
    value.startsWith("2001:db8:")
  ) return false;
  if (value.startsWith("::ffff:")) {
    const mapped = value.slice(7);
    return isIP(mapped) === 4 ? publicIPv4(mapped) : false;
  }
  return true;
}

function normalizeHostname(hostname) {
  return hostname.startsWith("[") && hostname.endsWith("]")
    ? hostname.slice(1, -1)
    : hostname;
}

function assertPublicAddress(address, family) {
  const valid = family === 4 ? publicIPv4(address) : family === 6 ? publicIPv6(address) : false;
  if (!valid) throw new Error("host_not_routable");
}

export async function resolvePublicHost(hostname, resolver = lookup) {
  const normalized = normalizeHostname(hostname);
  const literal = isIP(normalized);
  if (literal) {
    assertPublicAddress(normalized, literal);
    return [normalized];
  }
  if (normalized.toLowerCase() === "localhost") throw new Error("host_not_routable");
  const addresses = await resolver(normalized, { all: true, verbatim: true });
  if (!addresses.length) throw new Error("host_unresolvable");
  for (const entry of addresses) {
    assertPublicAddress(entry.address, entry.family);
  }
  return [...new Set(addresses.map(entry => entry.address))];
}

export function hostResolverRule(hostname, address) {
  const normalized = normalizeHostname(hostname);
  if (isIP(normalized)) return null;
  const family = isIP(address);
  if (!family) throw new Error("host_unresolvable");
  const mapped = family === 6 ? `[${address}]` : address;
  return `MAP ${normalized} ${mapped}, EXCLUDE localhost`;
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
