import { NextResponse, type NextRequest } from "next/server";

import { safeInternalPath } from "@/lib/redirects";
import { isAuthConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Where Supabase sends the browser back after an email confirmation or an
 * OAuth provider.
 *
 * A route handler rather than a page, because this is the one place that both
 * needs to exchange a code for a session *and* can set the resulting cookies:
 * a server component cannot write them.
 */
export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const next = safeInternalPath(url.searchParams.get("next"));

  if (!isAuthConfigured() || code === null) {
    return NextResponse.redirect(new URL("/login", url.origin));
  }

  const supabase = await createSupabaseServerClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    // No detail in the URL: the reason is for the log, not for a querystring
    // that ends up in browser history and referrer headers.
    return NextResponse.redirect(new URL("/login?error=exchange", url.origin));
  }

  return NextResponse.redirect(new URL(next, url.origin));
}
