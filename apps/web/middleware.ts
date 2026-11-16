/**
 * Refresh the Supabase session cookie on every request.
 *
 * Access tokens are short-lived. A server component can *read* a cookie but
 * cannot write one, so without this the token would expire mid-session and
 * every server-rendered page would start getting 401s from the API with no
 * way to recover except a manual reload. Middleware is the one place in the
 * App Router that can both read the request's cookies and set the response's.
 *
 * It deliberately does no authorization, and this file adding a second job
 * does not change that. Deciding what a visitor may see belongs to the pages,
 * which know what they are about to render; middleware sees a URL and
 * nothing else. The matcher below excludes Next's own assets and top-level
 * image files, which is not the same as running only where there is something
 * to protect: every page request arrives here, and so does every request for
 * a route that renders nothing. Nothing below redirects, refuses or inspects
 * a session.
 *
 * The second job is to say where the request was going. Middleware is the only
 * layer that knows: a layout gets its own params and its ancestors', never a
 * child's, and a server component has no pathname at all. So the validated
 * path and query travel on to the render in a request header, and
 * `requireSession` uses it to build the `?next=` on the sign-in redirect.
 * **A navigation hint, not a credential** - see `lib/redirects.ts`.
 */

import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { RETURN_TO_HEADER, returnPath } from "./lib/redirects";
import { readSupabaseConfig } from "./lib/supabase/config";

/**
 * The request's own headers, plus where it was headed.
 *
 * `set` rather than append, and unconditional: a client can send a header of
 * this name, and the value a render trusts has to be the one computed here
 * from `nextUrl`. Copied at the moment of use rather than once up front,
 * because `request.cookies.set` writes back into `request.headers` - measured,
 * not assumed - so a copy taken before a refresh would carry the stale cookie
 * and undo the refresh this file exists for.
 */
function forwarding(request: NextRequest): Headers {
  const headers = new Headers(request.headers);
  headers.set(RETURN_TO_HEADER, returnPath(request.nextUrl.pathname, request.nextUrl.search));
  return headers;
}

export async function middleware(request: NextRequest) {
  const config = readSupabaseConfig();
  // Nothing to refresh in a checkout with no Supabase project. The pages
  // render an "unconfigured" state instead of failing here - but the header
  // still goes with it, so this path cannot be the one that quietly drops a
  // visitor's destination.
  if (config === null) {
    return NextResponse.next({ request: { headers: forwarding(request) } });
  }

  let response = NextResponse.next({ request: { headers: forwarding(request) } });

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
        // Rebuilt, so the refreshed cookies *and* the header both survive:
        // this response replaces the one above, and anything missing here is
        // missing from the render.
        response = NextResponse.next({ request: { headers: forwarding(request) } });
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
    // The authenticated tree, unconditionally - because the entry above
    // excludes *any* path ending in an image extension, and a route parameter
    // is entitled to end that way. `/w/x.png` is a workspace id, not a
    // picture, and `/agents/agent.png` is an agent detail page. Measured
    // before this line existed: both skipped middleware, so a client-supplied
    // `x-agentworld-return-to` reached the render unreplaced.
    //
    // Added rather than narrowing the regex above, which stays byte for byte.
    // Next tries each entry and runs middleware on the first that matches
    // (`shared/lib/router/utils/middleware-route-matcher.js`), so entries are
    // a union with no precedence: this can only widen coverage, and cannot
    // make a path the first entry excludes - `/logo.png`, `/favicon.ico`,
    // `/_next/*` - start matching.
    "/w/:path*",
  ],
};
