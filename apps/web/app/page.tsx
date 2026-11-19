import Link from "next/link";
import { redirect } from "next/navigation";

import { readSession } from "@/lib/session";

/**
 * Rendered per request, never prerendered. Without this the session check is
 * evaluated once at build time and the result frozen into the output, so the
 * first visitor's view would be whatever the build server saw - which is
 * nobody, signed out, with no API.
 */
export const dynamic = "force-dynamic";

/**
 * The entry point, and the only page that has to handle every state.
 *
 * Each branch sends the visitor somewhere they can act: a setup instruction,
 * a sign-in form, their workspace, or an honest statement that the backend is
 * down. Nothing here is a dead end.
 *
 * **The return type is annotated on purpose.** Without it a `SessionState`
 * added later would fall off the end of this switch and the function would
 * return `undefined`, which React renders as an empty page with a 200 - a
 * silent dead end, and exactly what happened when `token-rejected` was added
 * and this file was not. Naming the type makes the next one a build error.
 */
export default async function StartPage(): Promise<React.ReactElement> {
  const session = await readSession();

  switch (session.kind) {
    case "unconfigured":
      return <Unconfigured />;
    case "forbidden":
      return <Forbidden message={session.message} requestId={session.requestId} />;
    case "api-unreachable":
      return <ApiDown message={session.message} requestId={session.requestId} />;
    // `return redirect(...)` rather than a bare call: `redirect` is typed
    // `never` because it works by throwing, but eslint cannot know that and
    // reads the next `case` as a fall-through. Returning it states the
    // control flow instead of adding an unreachable `break`.
    //
    // A session the API refuses goes to the same place as no session at all,
    // and the sign-in page is what tells the two apart: it renders for both,
    // and explains itself for the rejected one. It must not be folded in with
    // the branches above - this page *renders* for `api-unreachable`, so
    // sending a rejected token there would report a configuration problem as
    // an outage and leave the visitor nothing to do.
    case "signed-out":
    case "token-rejected":
      return redirect("/login");
    case "signed-in":
      // A user with no workspace is not an error state; it is the onboarding
      // screen. The API says so with `needs_onboarding` rather than leaving
      // the client to infer it from an empty list.
      if (session.me.needs_onboarding) return redirect("/onboarding");
      return redirect(`/w/${session.me.workspaces[0]!.workspace.id}`);
  }
}

function Unconfigured() {
  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">Almost there</h1>
        <p className="muted">
          AgentWorld is running, but no identity provider is configured, so there is
          nobody to sign in as yet.
        </p>
      </div>

      <div className="card">
        <h2 className="card__title">Connect Supabase Auth</h2>
        <ol className="steps">
          <li>
            Create a project at <code>supabase.com</code>, or point at a self-hosted one.
          </li>
          <li>
            Put its URL and <em>anon</em> key in <code>apps/web/.env.local</code> as{" "}
            <code>NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code>NEXT_PUBLIC_SUPABASE_ANON_KEY</code>. Next.js reads that file, not the
            <code>.env</code> at the repository root; start from{" "}
            <code>apps/web/.env.example</code>.
          </li>
          <li>
            Give the API the same project: <code>SUPABASE_URL</code> and{" "}
            <code>SUPABASE_JWKS_URL</code> (
            <code>&lt;url&gt;/auth/v1/.well-known/jwks.json</code>).
          </li>
          <li>Restart both.</li>
        </ol>
        <p className="muted">
          The anon key is meant to be public and ships to the browser. The service-role
          key is not, and AgentWorld never needs it.
        </p>
      </div>
    </section>
  );
}

/**
 * The API knows who you are and will not serve you.
 *
 * Deliberately not the sign-in form and deliberately not "the backend is
 * down". Signing in again produces the same token and the same refusal, and
 * the backend answering 403 is a backend that is working - so the only thing
 * on offer here is the API's own sentence, the request id to quote, and the
 * fact that somebody else has to act.
 */
function Forbidden({
  message,
  requestId,
}: {
  readonly message: string;
  readonly requestId?: string | undefined;
}) {
  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">This account cannot be served</h1>
        <p className="muted">
          You are signed in and the backend is answering - it is refusing this account
          rather than failing. Signing out and back in would produce the same token and
          the same answer.
        </p>
      </div>

      <div className="card">
        <h2 className="card__title">What the API said</h2>
        <p className="muted">{message}</p>
        {requestId !== undefined && (
          <p className="muted">
            Request id <code>{requestId}</code> - quote it when asking for access.
          </p>
        )}
        <p className="muted">
          Someone with access to this deployment has to grant it; there is nothing to
          change on this side.
        </p>
      </div>

      <p className="muted">
        <Link href="/">Try again</Link>
      </p>
    </section>
  );
}

function ApiDown({
  message,
  requestId,
}: {
  readonly message: string;
  readonly requestId?: string | undefined;
}) {
  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">The API is not answering</h1>
        <p className="muted">
          The web app is fine; it cannot reach the backend. These are separate services
          on purpose, so this is a normal thing to see while one of them restarts.
        </p>
      </div>

      <div className="card">
        <h2 className="card__title">What happened</h2>
        <p className="muted">{message}</p>
        {requestId !== undefined && (
          <p className="muted">
            Request id <code>{requestId}</code> - quote it when reporting this.
          </p>
        )}
        <p className="muted">
          Start it with <code>npm run api:dev</code>, and check{" "}
          <code>npm run infra:up</code> has Postgres running.
        </p>
      </div>

      <p className="muted">
        <Link href="/">Try again</Link>
      </p>
    </section>
  );
}
