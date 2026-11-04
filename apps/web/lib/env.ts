/**
 * Public runtime configuration.
 *
 * Only NEXT_PUBLIC_* values belong here: everything in this file is inlined
 * into the browser bundle. A secret placed here would ship to every visitor,
 * so the type below is deliberately narrow.
 */

export interface PublicEnv {
  readonly apiUrl: string;
  readonly wsUrl: string;
  readonly supabaseUrl: string;
  readonly supabaseAnonKey: string;
}

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(
      `${name} is not set. Copy apps/web/.env.example to apps/web/.env.local - ` +
        "Next.js reads .env files from this app's directory, not the repository root.",
    );
  }
  return value;
}

/** Throws on a missing value, so a misconfigured build fails before it ships. */
export function readPublicEnv(): PublicEnv {
  return {
    apiUrl: required("NEXT_PUBLIC_API_URL", process.env.NEXT_PUBLIC_API_URL),
    wsUrl: required("NEXT_PUBLIC_WS_URL", process.env.NEXT_PUBLIC_WS_URL),
    supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL ?? "",
    supabaseAnonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "",
  };
}

/**
 * Phase 0 default, so the shell renders before Supabase Auth exists (Phase 1).
 * Falls back rather than throwing, because a developer who has not copied
 * apps/web/.env.example to apps/web/.env.local yet should still see the app
 * boot. That fallback is also why putting these values in the root .env fails
 * quietly: Next reads nothing from there, so the app boots on the defaults
 * and the setup page reports no Supabase project.
 */
export const publicEnv: PublicEnv = {
  apiUrl: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  wsUrl: process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000",
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL ?? "",
  supabaseAnonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "",
};
