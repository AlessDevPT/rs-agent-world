/**
 * The Supabase client for the browser.
 *
 * Only the sign-in and sign-out pages use this. Everything else reads the
 * session on the server, so a page's data is fetched once during render
 * rather than after a round trip to decide who the viewer is.
 */

"use client";

import { createBrowserClient } from "@supabase/ssr";

import { requireSupabaseConfig } from "./config";

export function createSupabaseBrowserClient() {
  const { url, anonKey } = requireSupabaseConfig();
  return createBrowserClient(url, anonKey);
}
