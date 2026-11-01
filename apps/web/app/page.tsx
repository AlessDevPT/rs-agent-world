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
 */
export default async function StartPage() {
  const session = await readSession();

  switch (session.kind) {
    case "unconfigured":
      return <Unconfigured />;
    case "api-unreachable":
      return <ApiDown message={session.message} requestId={session.requestId} />;
    // `return redirect(...)` rather than a bare call: `redirect` is typed
    // `never` because it works by throwing, but eslint cannot know that and
    // reads the next `case` as a fall-through. Returning it states the
    // control flow instead of adding an unreachable `break`.
    case "signed-out":
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
            Put its URL and <em>anon</em> key in <code>.env</code> at the repository root,
            as <code>NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code>NEXT_PUBLIC_SUPABASE_ANON_KEY</code>.
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
