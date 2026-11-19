/**
 * What the route shows while the page's loads are in flight.
 *
 * The page is `force-dynamic` and fetches the project, the agent and - for an
 * admin - the workspace's model profiles before it can render a form. Static
 * text rather than a skeleton: an edit form drawn before the agent arrives
 * would be a set of empty boxes where the stored values are about to be, and
 * a blank box on this form means "cleared".
 */
export default function LoadingAgentEdit() {
  return (
    <section className="stack">
      <div>
        <h1 className="title">Edit agent</h1>
        <p className="muted" role="status">
          Loading this agent…
        </p>
      </div>
    </section>
  );
}
