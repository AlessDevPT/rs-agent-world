"use client";

import { useRef, useState, useTransition } from "react";

import { createProjectAction } from "@/app/actions";

export function CreateProjectForm({ workspaceId }: { readonly workspaceId: string }) {
  const formRef = useRef<HTMLFormElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function submit(form: FormData) {
    setError(null);
    startTransition(async () => {
      const result = await createProjectAction(workspaceId, form);
      if (result.ok) {
        // The action revalidates the page, so the new project appears in the
        // list above; clearing the form is all that is left to do here.
        formRef.current?.reset();
      } else {
        setError(result.message);
      }
    });
  }

  return (
    <form className="form" action={submit} ref={formRef}>
      <div className="form__row">
        <label className="form__label" htmlFor="project-name">
          Name
        </label>
        <input
          className="form__input"
          id="project-name"
          name="name"
          required
          maxLength={200}
          placeholder="EU Workers"
        />
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor="project-description">
          Description <span className="muted">(optional)</span>
        </label>
        <input
          className="form__input"
          id="project-description"
          name="description"
          maxLength={2000}
        />
      </div>

      {error !== null && (
        <p className="form__error" role="alert">
          {error}
        </p>
      )}

      <div className="form__actions">
        <button className="button button--primary" type="submit" disabled={pending}>
          {pending ? "Creating…" : "Create project"}
        </button>
      </div>
    </form>
  );
}
