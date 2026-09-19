import "server-only";
// Bound URL-encoded form bodies even when Content-Length is absent or misleading.
export async function readForm(request: Request): Promise<URLSearchParams> {
  if (!request.headers.get("content-type")?.startsWith("application/x-www-form-urlencoded")) throw new Error("Invalid form");
  const reader = request.body?.getReader();
  if (!reader) throw new Error("Missing form");
  const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      size += value.length; if (size > 4096) { await reader.cancel(); throw new Error("Form too large"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return new URLSearchParams(Buffer.concat(chunks).toString("utf8"));
}
