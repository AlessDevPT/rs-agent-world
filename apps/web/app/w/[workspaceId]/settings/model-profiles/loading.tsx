/**
 * What the route shows while the page's three loads are in flight.
 *
 * The page is `force-dynamic` and makes three server calls before it can
 * render anything, so without this the shell sits on the previous screen with
 * no sign that anything is happening. Static text rather than a skeleton of
 * the list: the number of profiles is exactly what is not known yet, and a
 * skeleton of three rows would be a guess rendered as data.
 */
export default function LoadingModelProfiles() {
  return (
    <section className="stack">
      <div>
        <h1 className="title">Model profiles</h1>
        <p className="muted" role="status">
          Loading the workspace&apos;s profiles…
        </p>
      </div>
    </section>
  );
}
