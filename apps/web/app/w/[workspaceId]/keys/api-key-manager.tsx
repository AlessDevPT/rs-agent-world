"use client";

import { useState, useTransition } from "react";

import { createApiKeyAction, revokeApiKeyAction } from "@/app/actions";
import type { ApiKey, ApiKeyScope, MintedApiKey, Project } from "@/lib/api/tenancy";

/**
 * Mint, show once, list and revoke.
 *
 * The show-once dialog is the whole reason this is a client component. The
 * plaintext key exists for the length of one server action's return value;
 * it is held in React state, shown, and dropped when the dialog closes. It is
 * never put in the URL, in `localStorage`, or in anything that outlives the
 * tab - all of which would quietly undo the property that the server cannot
 * recover it either.
 */
export function ApiKeyManager({
  workspaceId,
  initialKeys,
  projects,
}: {
  readonly workspaceId: string;
  readonly initialKeys: readonly ApiKey[];
  readonly projects: readonly Project[];
}) {
  const [minted, setMinted] = useState<MintedApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<ApiKeyScope>("ingest");
  const [pending, startTransition] = useTransition();

  function mint(form: FormData) {
    setError(null);
    startTransition(async () => {
      const result = await createApiKeyAction(workspaceId, form);
      if (result.ok) setMinted(result.minted);
      else setError(result.message);
    });
  }

  function revoke(apiKeyId: string) {
    setError(null);
    startTransition(async () => {
      const result = await revokeApiKeyAction(workspaceId, apiKeyId);
      if (!result.ok) setError(result.message);
    });
  }

  return (
    <div className="stack">
      {minted !== null && (
        <ShowOnce minted={minted} onDismiss={() => setMinted(null)} />
      )}

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {initialKeys.length === 0 ? (
        <div className="card">
          <h2 className="card__title">No keys yet</h2>
          <p className="muted">
            An <code>ingest</code> key is what a monitored agent uses. It has to be
            pinned to a project, so that a leaked key cannot reach the rest of the
            workspace.
          </p>
        </div>
      ) : (
        <ul className="list">
          {initialKeys.map((key) => (
            <li className="list__item" key={key.id}>
              <div>
                <span className="list__title">{key.name}</span>{" "}
                <code className="muted">{key.prefix}</code>
                <p className="muted">
                  {key.scope}
                  {key.project_id === null
                    ? " · workspace-wide"
                    : ` · ${projectName(projects, key.project_id)}`}
                  {key.agent_key_allowlist !== null &&
                    ` · agents ${key.agent_key_allowlist.join(", ")}`}
                  {" · "}
                  {key.last_used_at === null
                    ? "never used"
                    : `last used ${formatWhen(key.last_used_at)}`}
                </p>
              </div>
              <button
                className="button button--danger"
                type="button"
                disabled={pending}
                onClick={() => revoke(key.id)}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="card">
        <h2 className="card__title">New key</h2>
        <form className="form" action={mint}>
          <div className="form__row">
            <label className="form__label" htmlFor="key-name">
              Name
            </label>
            <input
              className="form__input"
              id="key-name"
              name="name"
              required
              maxLength={200}
              placeholder="EU workers"
            />
            <p className="form__hint">
              For you, not for the agent. It is what you will recognise when revoking.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="key-scope">
              Scope
            </label>
            <select
              className="form__input"
              id="key-scope"
              name="scope"
              value={scope}
              onChange={(event) => setScope(event.target.value as ApiKeyScope)}
            >
              <option value="ingest">ingest — emit events, read own rows</option>
              <option value="control">control — manage tasks and agents</option>
              <option value="full">full — both</option>
            </select>
            <p className="form__hint">
              No key of any scope can mint further keys: one leak must not become
              permanent access.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="key-project">
              Project
            </label>
            <select
              className="form__input"
              id="key-project"
              name="project_id"
              required={scope === "ingest"}
              defaultValue={projects[0]?.id ?? ""}
            >
              {scope !== "ingest" && <option value="">Whole workspace</option>}
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            {scope === "ingest" && projects.length === 0 && (
              <p className="form__hint">
                An ingest key must be pinned to a project, and this workspace has none
                yet.
              </p>
            )}
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="key-allowlist">
              Agent keys <span className="muted">(optional)</span>
            </label>
            <input
              className="form__input"
              id="key-allowlist"
              name="agent_key_allowlist"
              placeholder="etl-worker-eu-*, planner"
            />
            <p className="form__hint">
              Comma-separated. A single trailing <code>*</code> is the only pattern
              supported. Leave empty to allow any agent in the project.
            </p>
          </div>

          <div className="form__actions">
            <button
              className="button button--primary"
              type="submit"
              disabled={pending || (scope === "ingest" && projects.length === 0)}
            >
              {pending ? "Minting…" : "Mint key"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ShowOnce({
  minted,
  onDismiss,
}: {
  readonly minted: MintedApiKey;
  readonly onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(minted.key);
      setCopied(true);
    } catch {
      // Clipboard access needs a secure context and can be refused. The key
      // is on screen and selectable, so there is nothing to recover from -
      // only a button that should stop claiming it worked.
      setCopied(false);
    }
  }

  return (
    <div className="card card--highlight" role="alert" aria-live="assertive">
      <h2 className="card__title">Copy this key now</h2>
      <p className="muted">
        This is the only time <strong>{minted.name}</strong> can be shown. The server
        holds an Argon2id hash of the secret half and cannot reproduce it - if you lose
        it, mint another.
      </p>

      <pre className="secret">
        <code>{minted.key}</code>
      </pre>

      <div className="form__actions">
        <button className="button button--primary" type="button" onClick={copy}>
          {copied ? "Copied" : "Copy to clipboard"}
        </button>
        <button className="button button--quiet" type="button" onClick={onDismiss}>
          I have saved it
        </button>
      </div>
    </div>
  );
}

function projectName(projects: readonly Project[], projectId: string): string {
  return projects.find((project) => project.id === projectId)?.name ?? "unknown project";
}

function formatWhen(iso: string): string {
  // The server's ISO timestamp, rendered in the viewer's locale. Rendered in
  // a client component on purpose: doing it on the server would use the
  // server's timezone and then hydrate into a mismatch.
  return new Date(iso).toLocaleString();
}
