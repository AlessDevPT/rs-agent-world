"use client";

import { useState, useTransition } from "react";

import { createWorkspaceAction } from "@/app/actions";

/**
 * The create-workspace form.
 *
 * The slug field is optional and shows what will be derived from the name, so
 * the common case is one field and the URL is still not a surprise.
 */
export function CreateWorkspaceForm() {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const derived = slugify(name);

  function submit(form: FormData) {
    setError(null);
    startTransition(async () => {
      const result = await createWorkspaceAction(form);
      // A success redirects, so only a failure returns anything at all.
      if (result !== undefined && !result.ok) setError(result.message);
    });
  }

  return (
    <form className="form" action={submit}>
      <div className="form__row">
        <label className="form__label" htmlFor="name">
          Name
        </label>
        <input
          className="form__input"
          id="name"
          name="name"
          required
          maxLength={200}
          placeholder="Support Automation"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor="slug">
          URL slug <span className="muted">(optional)</span>
        </label>
        <input
          className="form__input"
          id="slug"
          name="slug"
          maxLength={48}
          placeholder={derived || "support-automation"}
          value={slug}
          onChange={(event) => setSlug(event.target.value)}
        />
        {slug === "" && derived !== "" && (
          <p className="form__hint">
            Will be <code>{derived}</code>
          </p>
        )}
      </div>

      {error !== null && (
        <p className="form__error" role="alert">
          {error}
        </p>
      )}

      <div className="form__actions">
        <button className="button button--primary" type="submit" disabled={pending}>
          {pending ? "Creating…" : "Create workspace"}
        </button>
      </div>
    </form>
  );
}

/**
 * A preview of what the server will derive. Deliberately not the real rule -
 * the API owns that, including the reserved words - so this only ever shows a
 * hint, and a mismatch surfaces as the API's own error message rather than
 * being hidden by a client-side check that disagreed.
 */
function slugify(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/g, "");
}
