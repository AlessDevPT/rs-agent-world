import { getMeta, type Meta } from "@/lib/api/client";

/**
 * Rendered per request, never prerendered. Without this the API check is
 * evaluated once at build time and the result frozen into the output, so a
 * build run while the API was down would report "unreachable" forever.
 */
export const dynamic = "force-dynamic";

/**
 * Phase 0 dashboard.
 *
 * It does one real thing: reach the API and report what it found. That makes
 * the frontend/backend split (goal G7) observable from the first commit -
 * if this card shows an error, the two halves are genuinely not talking, and
 * nothing is faking agreement.
 */
export default async function OverviewPage() {
  let meta: Meta | null = null;
  let error: string | null = null;

  try {
    meta = await getMeta();
  } catch (e) {
    error = e instanceof Error ? e.message : "Unknown error";
  }

  return (
    <section className="stack">
      <div>
        <h1 className="title">Overview</h1>
        <p className="muted">
          No agents are connected yet. Agent registration and the live island arrive in
          later phases; this page currently proves the API connection.
        </p>
      </div>

      <div className="card">
        <h2 className="card__title">API connection</h2>
        {meta ? (
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              <span className="pill pill--ok">connected</span>
            </dd>
            <dt>API version</dt>
            <dd>{meta.api_version}</dd>
            <dt>Protocol version</dt>
            <dd>{meta.protocol_version}</dd>
            <dt>Adapters</dt>
            <dd>{meta.adapters.length > 0 ? meta.adapters.join(", ") : "none registered"}</dd>
            <dt>Server time</dt>
            <dd>
              <code>{meta.server_time}</code>
            </dd>
          </dl>
        ) : (
          <div className="stack">
            <span className="pill pill--bad">unreachable</span>
            <p className="muted">{error}</p>
            <p className="muted">
              Start the API with <code>npm run api:dev</code> from the repository root.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
