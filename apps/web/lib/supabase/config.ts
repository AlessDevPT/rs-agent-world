/**
 * Is Supabase Auth configured, and with what.
 *
 * A fresh checkout has no Supabase project, and that has to be a *state* the
 * app can render rather than a crash: `createBrowserClient` throws on an empty
 * URL, so every page that touched auth would 500 and the first thing a new
 * contributor saw would be a stack trace instead of an instruction.
 *
 * Both values are public by design - the anon key is meant to ship to the
 * browser, and row-level security in Supabase is what makes that safe. No
 * secret belongs in this file or anything it imports.
 */

export interface SupabaseConfig {
  readonly url: string;
  readonly anonKey: string;
}

export function readSupabaseConfig(): SupabaseConfig | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";
  // Both or neither. One of the two set is a half-finished .env, which would
  // otherwise fail later and less clearly.
  if (!url || !anonKey) return null;
  return { url, anonKey };
}

export function isAuthConfigured(): boolean {
  return readSupabaseConfig() !== null;
}

/** For the paths that cannot proceed without it, and should say so plainly. */
export function requireSupabaseConfig(): SupabaseConfig {
  const config = readSupabaseConfig();
  if (config === null) {
    throw new Error(
      "Supabase Auth is not configured. Set NEXT_PUBLIC_SUPABASE_URL and " +
        "NEXT_PUBLIC_SUPABASE_ANON_KEY in .env at the repository root.",
    );
  }
  return config;
}
