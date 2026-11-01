/**
 * Validating a redirect target that came from a query string.
 *
 * The sign-in flow carries `?next=` so that signing in returns the visitor to
 * where they were headed. That value is attacker-controlled, and a sign-in
 * page is the single most useful place to have an open redirect: a link to
 * our own domain that bounces to theirs, arriving immediately after the
 * visitor typed a password, is a convincing way to collect another one.
 *
 * So: same-site absolute paths only. Everything else becomes "/".
 */

/** The fallback for anything that does not pass. */
export const HOME = "/";

export function safeInternalPath(value: string | null | undefined): string {
  if (!value) return HOME;

  // Must be an absolute path on this origin.
  if (!value.startsWith("/")) return HOME;

  // "//evil.test" and "/\evil.test" are protocol-relative URLs: a browser
  // reads both as "go to another host", and the second slips past a check
  // that only looks for a doubled forward slash.
  if (value.startsWith("//") || value.startsWith("/\\")) return HOME;

  // A control character can be used to break a Location header across lines
  // and inject one of the attacker's own. Checked by code point rather than
  // with a character class: literal control characters in a regex are what
  // `no-control-regex` exists to catch, and a suppression comment here would
  // read as "this rule is inconvenient" rather than as a deliberate check.
  for (let position = 0; position < value.length; position += 1) {
    const code = value.charCodeAt(position);
    if (code <= 0x1f || code === 0x7f) return HOME;
  }

  return value;
}
