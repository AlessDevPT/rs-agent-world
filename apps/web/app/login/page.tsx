import Link from "next/link";
import { redirect } from "next/navigation";

import { loginDestination, returnTarget } from "@/lib/auth/destination";
import { readSession } from "@/lib/session";

import { SignInForm } from "./sign-in-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "Sign in · AgentWorld" };

/**
 * The sign-in page.
 *
 * `next` carries where the visitor was headed before being sent here, so
 * signing in returns them to it. It is validated as a same-site path before
 * use: an unchecked redirect target taken from a query string is an open
 * redirect, and a sign-in page is exactly where one is most useful to an
 * attacker. `/login` and `/auth/*` are refused on top of that, because this
 * page now returns a signed-in visitor to `next` rather than always to `/`,
 * and a `next` pointing back here would be a loop.
 *
 * **It asks `readSession()` rather than reading the cookie.** It used to do
 * the latter - `getAccessToken() !== null` and redirect to `/` - which is a
 * different question from the one every other page asks, and the two could
 * disagree. When the API rejected a token the cookie still held, this page
 * said "already signed in" and `/` said "signed out", and the browser bounced
 * between them until it gave up. Measured, twelve hops, no `Set-Cookie` on any
 * of them. So there is one question now, and `lib/auth/destination.ts` turns
 * its answer into what this page does.
 */
export default async function LoginPage({
  searchParams,
}: {
  readonly searchParams: Promise<{ next?: string; error?: string }>;
}) {
  const { next, error } = await searchParams;
  const session = await readSession();

  const destination = loginDestination(session.kind, next ?? null);
  if (destination.kind === "redirect") redirect(destination.to);

  const target = returnTarget(next ?? null);
  const rejected = session.kind === "token-rejected";

  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">Sign in</h1>
        <p className="muted">
          Accounts live in Supabase Auth. AgentWorld never sees your password - the form
          below talks to Supabase directly.
        </p>
      </div>

      {rejected && <StaleSession next={target} />}

      {/* The one thing the callback route can fail at. It sends no detail in
          the URL - the reason is for the log, not for a querystring that ends
          up in browser history - so this says what it can and no more. */}
      {error === "exchange" && (
        <div className="card">
          <p className="form__error" role="alert">
            That sign-in link could not be completed. It may have been used already or
            expired. Signing in below works just as well.
          </p>
        </div>
      )}

      <div className="card">
        <SignInForm next={target} />
      </div>

      <p className="muted">
        <Link href="/">Back to the start</Link>
      </p>
    </section>
  );
}

/**
 * What to say when the browser has a session this server will not accept.
 *
 * Deliberately specific about whose problem it is. The visitor did nothing
 * wrong and signing in again may not help them: the usual cause is this
 * deployment's API being configured for a different Supabase project, or for a
 * different audience or issuer, and a fresh token from the same project would
 * be refused in exactly the same way. So it names the two things that do help
 * - signing out, and telling whoever runs the server - rather than implying a
 * retry will fix it.
 *
 * No request id, no status, no detail from the API. A sign-in page is reachable
 * by anyone, and what it says about how the server is configured should be the
 * minimum that lets an honest visitor act.
 */
function StaleSession({ next }: { readonly next: string }) {
  return (
    <div className="card">
      <h2 className="card__title">This browser&apos;s sign-in is not valid here</h2>
      <p className="muted">
        You are signed in to the identity provider, but this server will not accept that
        session. Signing in again may not change that - if it does not, the server and the
        identity provider are configured for different projects, and whoever runs it needs
        to know.
      </p>
      {/* A POST, like the sign-out button in the workspace bar: no other site
          should be able to sign a visitor out with an image tag. `next` rides
          along so that recovering does not also lose where the visitor was
          headed.

          The label says "everywhere" because that is what the route does: it
          signs out with the library's global scope, which revokes this
          account's sessions on the visitor's other devices too. "Clear this
          session" would describe a smaller act than the button performs.

          The copy says "once their current session expires" on purpose. A
          global sign-out revokes refresh tokens; it does not invalidate
          access tokens already issued, and this API verifies those locally
          against JWKS with no revocation check - so another device keeps
          working until its own token runs out and the refresh fails. */}
      <form action="/auth/signout" method="post">
        <input type="hidden" name="next" value={next} />
        <button className="button button--quiet" type="submit">
          Sign out everywhere
        </button>
      </form>
      <p className="muted">
        This is the same sign-out the app itself uses, so your other devices are signed
        out too, once their current session expires.
      </p>
    </div>
  );
}
