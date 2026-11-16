import { NextResponse, type NextRequest } from "next/server";

import { guardDestination, returnTarget } from "@/lib/auth/destination";
import { HOME } from "@/lib/redirects";
import { isAuthConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Sign out, and send the visitor to the sign-in page.
 *
 * POST only. A GET would let any page sign a visitor out with an `<img>` tag,
 * which is a nuisance rather than a vulnerability but trivially avoidable.
 *
 * **This is also the recovery route**, which is why it takes a `next`: a
 * browser holding a session the API refuses has no way out of a server render,
 * because a server component cannot write a cookie and only a route handler or
 * middleware can. The sign-in page offers this, and the destination the
 * visitor was originally headed for rides along so that recovering from a bad
 * cookie does not also cost them their deep link.
 *
 * The sign-out scope is left at the library's `"global"` default, which is
 * what the workspace "Sign out" button has always done. A local-only sign-out
 * would serve this route's recovery use better - all it needs is this
 * browser's cookie gone - but narrowing it would change what that existing
 * button does, and that is a decision of its own rather than part of fixing a
 * redirect loop.
 */
export async function POST(request: NextRequest) {
  const target = returnTarget(await requestedNext(request));

  if (isAuthConfigured()) {
    const supabase = await createSupabaseServerClient();
    // The result is deliberately not inspected. `signOut` clears the local
    // session when the provider answers 401, 403 or 404, but on a network
    // failure it returns early and leaves the cookie in place - so this cannot
    // promise the cookie is gone. It does not need to: the sign-in page
    // renders either way, and says the same thing again if it is still there.
    await supabase.auth.signOut();
  }

  // The same function every guarded page uses, so a sign-out lands on exactly
  // the URL a guard would have produced. `null` for the ordinary sign-out
  // button, which sends no `next` and should not acquire one.
  const destination = guardDestination("signed-out", target === HOME ? null : target);

  // 303, not the 307 that `NextResponse.redirect` defaults to: a 307 preserves
  // the method, so the browser would POST to the destination.
  return NextResponse.redirect(new URL(destination.to, new URL(request.url).origin), 303);
}

/**
 * The `next` this request is asking for, unvalidated.
 *
 * The form body first, since that is how the sign-in page's recovery button
 * sends it, then the query string. Either way it is attacker-controlled and
 * goes through `returnTarget` before it reaches a `Location` header.
 */
async function requestedNext(request: NextRequest): Promise<string | null> {
  try {
    const form = await request.formData();
    const value = form.get("next");
    if (typeof value === "string") return value;
  } catch {
    // Not a form-encoded body. The query string is the other way to ask, and
    // a sign-out with neither is the ordinary case rather than an error.
  }
  return new URL(request.url).searchParams.get("next");
}
