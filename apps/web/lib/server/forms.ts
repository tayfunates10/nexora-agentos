import "server-only";
// Bound URL-encoded form bodies even when Content-Length is absent or misleading.
// Settings forms keep the small default; a form that carries agent instructions or a
// knowledge document states its own bound, which still has to be an explicit number.
export const SMALL_FORM_BYTES = 4096;
export const TEXT_FORM_BYTES = 262_144;

export async function readForm(request: Request, maxBytes = SMALL_FORM_BYTES): Promise<URLSearchParams> {
  if (!request.headers.get("content-type")?.startsWith("application/x-www-form-urlencoded")) throw new Error("Invalid form");
  const reader = request.body?.getReader();
  if (!reader) throw new Error("Missing form");
  const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      size += value.length; if (size > maxBytes) { await reader.cancel(); throw new Error("Form too large"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return new URLSearchParams(Buffer.concat(chunks).toString("utf8"));
}
