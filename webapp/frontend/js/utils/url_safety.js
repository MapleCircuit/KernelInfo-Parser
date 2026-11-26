/**
 * URL scheme validation helper for untrusted external links.
 */

const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;

/**
 * Validate an untrusted URL and allow only explicit http(s) schemes.
 * Returns the trimmed URL string if valid, otherwise null.
 */
export function safeHttpUrl(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed || CONTROL_CHAR_RE.test(trimmed)) return null;
  if (!/^https?:\/\//i.test(trimmed)) return null;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return trimmed;
  } catch {
    return null;
  }
}
