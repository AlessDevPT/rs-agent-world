/**
 * Refresh the Supabase session cookie on every request.
 *
 * Access tokens are short-lived. A server component can *read* a cookie but
 * cannot write one, so without this the token would expire mid-session and
 * every server-rendered page would start getting 401s from the API with no
 * way to recover except a manual reload. Middleware is the one place in the
 * App Router that can both read the request's cookies and set the response's.
 *
 * It deliberately does no authorization. Deciding what a visitor may see
 * belongs to the pages, which know what they are about to render; middleware
 * runs on every asset request and is the wrong place for policy.
 */

import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { readSupabaseConfig } from "./lib/supabase/config";

export async function middleware(request: NextRequest) {
  const config = readSupabaseConfig();
  // Nothing to refresh in a checkout with no Supabase project. The pages
  // render an "unconfigured" state instead of failing here.
  if (config === null) return NextResponse.next();

  let response = NextResponse.next({ request });

  const supabase = createServerClient(config.url, config.anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(toSet) {
        // Written to both: the request copy so a later read in this same pass
        // sees the new value, and the response so the browser keeps it.
        for (const { name, value } of toSet) {
          request.cookies.set(name, value);
        }
        response = NextResponse.next({ request });
        for (const { name, value, options } of toSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  // The call itself is the point: it refreshes an expiring token and triggers
  // `setAll` above. The user object is not needed here.
  await supabase.auth.getUser();

  return response;
}

export const config = {
  matcher: [
    // Everything except Next's own assets and obvious static files. Running
    // on an image request would refresh tokens dozens of times per page.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
