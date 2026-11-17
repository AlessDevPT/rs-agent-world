"use client";

import { useState, useTransition } from "react";

import {
  createCredentialAction,
  deleteCredentialAction,
  revokeCredentialAction,
  rotateCredentialAction,
} from "@/app/actions";
import { pillClassName } from "@/lib/agents/presentation";
import type { Credential, CredentialKind } from "@/lib/api/credentials";
import type { Project } from "@/lib/api/tenancy";
import {
  credentialState,
  credentialStateLabel,
  credentialStateTone,
  credentialsEmptyState,
  expirySentence,
  hintSentence,
  lastUsedSentence,
} from "@/lib/credentials/presentation";

/**
 * Store, list, rotate, revoke and destroy.
 *
 * **There is no show-once dialog here, and its absence is the point.** The
 * API-key manager has one because AgentWorld mints that secret and can
 * display it exactly once; a credential's material comes *from* the operator,
 * who already has it, and no route in this system will ever return it. So the
 * only direction material moves in this component is out of an input and into
 * a server action - and the input is cleared the moment the action succeeds,
 * because a key left sitting in a form is a key sitting in a tab somebody
 * walks away from.
 *
 * A client component for the same reason the key manager is one: the form
 * needs pending state, a per-row rotate panel, and a confirm step before
 * destroying material that cannot be recovered.
 */
export function CredentialManager({
  workspaceId,
  credentials,
  projects,
}: {
  readonly workspaceId: string;
  readonly credentials: readonly Credential[];
  readonly projects: readonly Project[];
}) {
  const [error, setError] = useState<string | null>(null);
  const [stored, setStored] = useState<string | null>(null);
  const [kind, setKind] = useState<CredentialKind>("api_key");
  const [rotating, setRotating] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function store(form: FormData) {
    setError(null);
    setStored(null);
    startTransition(async () => {
      const result = await createCredentialAction(workspaceId, form);
      if (result.ok) {
        setStored(result.credential.name);
        // The material is gone from this tab the moment it is stored. The
        // form element is the only place it existed in the browser.
        form.set("secret", "");
      } else {
        setError(result.message);
      }
    });
  }

  function rotate(credentialId: string, form: FormData) {
    setError(null);
    startTransition(async () => {
      const result = await rotateCredentialAction(workspaceId, credentialId, form);
      if (result.ok) setRotating(null);
      else setError(result.message);
    });
  }

  function revoke(credentialId: string) {
    setError(null);
    startTransition(async () => {
      const result = await revokeCredentialAction(workspaceId, credentialId);
      if (!result.ok) setError(result.message);
    });
  }

  function destroy(credentialId: string) {
    setError(null);
    startTransition(async () => {
      const result = await deleteCredentialAction(workspaceId, credentialId);
      if (result.ok) setConfirmingDelete(null);
      else setError(result.message);
    });
  }

  const now = new Date();
  const empty = credentialsEmptyState();

  return (
    <div className="stack">
      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {stored !== null && (
        <div className="card">
          <p role="status">
            Stored <strong>{stored}</strong>. The material is encrypted and cannot be read
            back; rotate it here if you need to replace it.
          </p>
        </div>
      )}

      <div className="card">
        <h2 className="card__title">Stored credentials</h2>
        {credentials.length === 0 ? (
          <>
            <p>{empty.headline}</p>
            <p className="muted">{empty.detail}</p>
          </>
        ) : (
          <ul className="list">
            {credentials.map((credential) => {
              const state = credentialState(credential, now);
              return (
                <li className="list__item" key={credential.id}>
                  <div>
                    <span className="list__title">{credential.name}</span>{" "}
                    <span className="muted">
                      <code>{credential.provider_slug}</code> · {credential.kind}
                    </span>
                    <p className="muted">{hintSentence(credential)}</p>
                    <p className="muted">
                      {expirySentence(credential)} · {lastUsedSentence(credential)}
                      {credential.project_id !== null && " · pinned to one project"}
                    </p>

                    {rotating === credential.id && (
                      <form
                        className="form"
                        action={(form) => {
                          rotate(credential.id, form);
                        }}
                      >
                        <div className="form__row">
                          <label className="form__label" htmlFor={`rotate-${credential.id}`}>
                            Replacement material
                          </label>
                          <input
                            className="form__input"
                            id={`rotate-${credential.id}`}
                            name="secret"
                            type="password"
                            autoComplete="off"
                            required
                          />
                          <p className="form__hint">
                            The id and the name stay as they are, so a model profile
                            pointing at this credential keeps working.
                          </p>
                        </div>
                        <div className="form__actions">
                          <button className="button" type="submit" disabled={pending}>
                            Replace material
                          </button>{" "}
                          <button
                            className="button button--quiet"
                            type="button"
                            onClick={() => {
                              setRotating(null);
                            }}
                          >
                            Cancel
                          </button>
                        </div>
                      </form>
                    )}

                    {confirmingDelete === credential.id && (
                      <p className="form__error" role="alert">
                        Destroying the material cannot be undone, and anything pointing at
                        this credential stops working.{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            destroy(credential.id);
                          }}
                        >
                          Destroy it
                        </button>{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          onClick={() => {
                            setConfirmingDelete(null);
                          }}
                        >
                          Keep it
                        </button>
                      </p>
                    )}
                  </div>

                  <div>
                    <span className={pillClassName(credentialStateTone(state))}>
                      {credentialStateLabel(state)}
                    </span>{" "}
                    {state !== "revoked" && (
                      <>
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            setRotating(rotating === credential.id ? null : credential.id);
                          }}
                        >
                          Rotate
                        </button>{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            revoke(credential.id);
                          }}
                        >
                          Revoke
                        </button>{" "}
                      </>
                    )}
                    <button
                      className="button button--quiet"
                      type="button"
                      disabled={pending}
                      onClick={() => {
                        setConfirmingDelete(
                          confirmingDelete === credential.id ? null : credential.id,
                        );
                      }}
                    >
                      Destroy
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Store a credential</h2>
        <form className="form" action={store}>
          <div className="form__row">
            <label className="form__label" htmlFor="credential-name">
              Name
            </label>
            <input
              className="form__input"
              id="credential-name"
              name="name"
              required
              maxLength={200}
              placeholder="Anthropic, production"
            />
            <p className="form__hint">
              How you will recognise it here. It is not sent to the provider.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-provider">
              Provider
            </label>
            <input
              className="form__input"
              id="credential-provider"
              name="provider_slug"
              required
              maxLength={100}
              placeholder="anthropic"
            />
            <p className="form__hint">
              The provider&apos;s slug, as a model profile will spell it. Nothing checks
              the two against each other yet, so a typo here surfaces when an agent runs.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-kind">
              Kind
            </label>
            <select
              className="form__input"
              id="credential-kind"
              name="kind"
              value={kind}
              onChange={(event) => {
                setKind(event.target.value as CredentialKind);
              }}
            >
              <option value="api_key">API key</option>
              <option value="oauth2">OAuth2 token set</option>
              <option value="basic">Username and password</option>
              <option value="custom">Something else</option>
            </select>
            <p className="form__hint">
              {kind === "api_key"
                ? "The last four characters will be shown in the list, the way the provider's own dashboard does."
                : "Only API-key material gets a hint in the list; the rest would give away more than they show."}
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-secret">
              Material
            </label>
            <input
              className="form__input"
              id="credential-secret"
              name="secret"
              type="password"
              autoComplete="off"
              required
            />
            <p className="form__hint">
              Encrypted under a key of its own before the row is written. No endpoint
              returns it and no role can read it back - if you lose it, rotate it.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-project">
              Project
            </label>
            <select className="form__input" id="credential-project" name="project_id">
              <option value="">The whole workspace</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            <p className="form__hint">
              Workspace-wide is the usual case. A model profile is workspace-scoped, so a
              credential pinned to one project is for something narrower.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-expires">
              Expires
            </label>
            <input
              className="form__input"
              id="credential-expires"
              name="expires_at"
              type="date"
            />
            <p className="form__hint">
              Optional, and only a reminder: AgentWorld stops using it after this date,
              but the provider decides whether the key itself still works.
            </p>
          </div>

          <div className="form__row">
            <label className="form__label" htmlFor="credential-scopes">
              Scopes
            </label>
            <input
              className="form__input"
              id="credential-scopes"
              name="scopes"
              placeholder="messages:write, models:read"
            />
            <p className="form__hint">
              Optional, comma separated. Recorded for your reference; nothing here
              enforces them.
            </p>
          </div>

          <div className="form__actions">
            <button className="button" type="submit" disabled={pending}>
              {pending ? "Storing…" : "Store credential"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
