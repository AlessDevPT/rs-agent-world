/**
 * What the route shows while the page's loads are in flight.
 *
 * The page is `force-dynamic` and fetches the project - and, for an admin, the
 * workspace's model profiles - before it can render a form that tells the
 * truth about what is available. Static text rather than a skeleton of the
 * form: a greyed-out copy of fields whose choices are not known yet would be
 * a guess rendered as data.
 */
export default function LoadingNewAgent() {
  return (
    <section className="stack">
      <div>
        <h1 className="title">New agent</h1>
        <p className="muted" role="status">
          Loading this project&apos;s settings…
        </p>
      </div>
    </section>
  );
}
