"use client";

import { useState, useTransition } from "react";

import {
  createModelProfileAction,
  deleteModelProfileAction,
  updateModelProfileAction,
} from "@/app/actions";
import { pillClassName } from "@/lib/agents/presentation";
import type { Credential } from "@/lib/api/credentials";
import type { ModelProfile } from "@/lib/api/model-profiles";
import {
  availabilityHint,
  createBody,
  credentialAfterProviderChange,
  credentialAvailability,
  credentialLabel,
  credentialSummary,
  defaultHint,
  draftFieldFor,
  draftOf,
  profileEdits,
  profileWarning,
  profilesEmptyState,
  providerChoices,
  type ProfileDraft,
} from "@/lib/model-profiles/presentation";

/**
 * List, create, edit, re-default and delete.
 *
 * A client component for the reasons the credential manager is one: pending
 * state, a per-row panel, and a confirm step before a delete. What it is
 * deliberately *not* is the place any rule lives - every decision it makes is
 * a call into `lib/model-profiles/presentation.ts`, because this file cannot
 * be asserted on by a Vitest that runs in node with no jsdom, and a rule that
 * cannot be tested is a rule that drifts.
 *
 * **Three states are kept apart throughout: "none", "none for this provider"
 * and "we could not ask".** The credential list is `null` for a viewer (the
 * API's listing is admin-only) and for an admin whose load failed, and
 * rendering either as "this workspace has no credentials" would send somebody
 * to create a key they already have.
 */
export function ModelProfileManager({
  workspaceId,
  profiles,
  providers,
  credentials,
  mayManage,
}: {
  readonly workspaceId: string;
  readonly profiles: readonly ModelProfile[];
  readonly providers: readonly string[] | null;
  readonly credentials: readonly Credential[] | null;
  readonly mayManage: boolean;
}) {
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const hasDefault = profiles.some((profile) => profile.is_default);

  function setDefault(profile: ModelProfile, isDefault: boolean) {
    setError(null);
    setNote(null);
    startTransition(async () => {
      const result = await updateModelProfileAction(workspaceId, profile.id, {
        is_default: isDefault,
      });
      if (result.ok) {
        setNote(
          isDefault
            ? `'${result.profile.name}' is now the workspace default.`
            : `'${result.profile.name}' is no longer the default. This workspace has none.`,
        );
      } else {
        setError(result.message);
      }
    });
  }

  function destroy(profile: ModelProfile) {
    setError(null);
    setNote(null);
    startTransition(async () => {
      const result = await deleteModelProfileAction(workspaceId, profile.id);
      if (result.ok) {
        setConfirmingDelete(null);
        setNote(`Deleted '${profile.name}'.`);
      } else {
        // The row stays on screen: a profile the API refused to delete - one
        // an agent still uses - has not gone anywhere, and removing it here
        // would be a lie the next page load corrects.
        setError(result.message);
      }
    });
  }

  const empty = profilesEmptyState();

  return (
    <div className="stack">
      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {note !== null && (
        <div className="card">
          <p role="status">{note}</p>
        </div>
      )}

      <div className="card">
        <h2 className="card__title">Profiles</h2>
        {profiles.length === 0 ? (
          <>
            <p>{empty.headline}</p>
            <p className="muted">{empty.detail}</p>
          </>
        ) : (
          <ul className="list">
            {profiles.map((profile) => {
              const warning = profileWarning(profile, providers, credentials);
              return (
                <li className="list__item" key={profile.id}>
                  <div>
                    <span className="list__title">{profile.name}</span>{" "}
                    <span className="muted">
                      <code>{profile.provider_slug}</code> · <code>{profile.model_id}</code>
                    </span>
                    <p className="muted">{credentialSummary(profile, credentials)}</p>
                    {warning !== null && (
                      <p className="form__error" role="alert">
                        {warning}
                      </p>
                    )}

                    {mayManage && editing === profile.id && (
                      <ProfileEditor
                        workspaceId={workspaceId}
                        profile={profile}
                        providers={providers}
                        credentials={credentials}
                        hasOtherDefault={hasDefault && !profile.is_default}
                        onDone={(message) => {
                          setEditing(null);
                          setNote(message);
                        }}
                      />
                    )}

                    {mayManage && confirmingDelete === profile.id && (
                      <p className="form__error" role="alert">
                        Deleting this profile cannot be undone. An agent still set to run on
                        it keeps the profile alive: the API refuses rather than leaving that
                        agent with no model.{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            destroy(profile);
                          }}
                        >
                          Delete it
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
                    {profile.is_default && (
                      <>
                        <span className={pillClassName("ok")}>default</span>{" "}
                      </>
                    )}
                    {mayManage && (
                      <>
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            setDefault(profile, !profile.is_default);
                          }}
                        >
                          {profile.is_default ? "Clear default" : "Make default"}
                        </button>{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            setError(null);
                            setEditing(editing === profile.id ? null : profile.id);
                          }}
                        >
                          {editing === profile.id ? "Close" : "Edit"}
                        </button>{" "}
                        <button
                          className="button button--quiet"
                          type="button"
                          disabled={pending}
                          onClick={() => {
                            setConfirmingDelete(
                              confirmingDelete === profile.id ? null : profile.id,
                            );
                          }}
                        >
                          Delete
                        </button>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {mayManage && (
        <div className="card">
          <h2 className="card__title">Add a profile</h2>
          {providers === null ? (
            <p className="muted">
              The provider list could not be loaded from the API, and a profile may only
              name a provider this build serves - so there is nothing safe to offer here.
              Reload the page to try again.
            </p>
          ) : providers.length === 0 ? (
            <p className="muted">
              This build serves no model providers, so a profile could name none of them.
            </p>
          ) : (
            <CreateProfileForm
              workspaceId={workspaceId}
              providers={providers}
              credentials={credentials}
              hasOtherDefault={hasDefault}
              onCreated={(message) => {
                setNote(message);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** Where a message belongs: against one field, or above the form. */
interface FormError {
  readonly message: string;
  readonly field: keyof ProfileDraft | null;
}

function CreateProfileForm({
  workspaceId,
  providers,
  credentials,
  hasOtherDefault,
  onCreated,
}: {
  readonly workspaceId: string;
  readonly providers: readonly string[];
  readonly credentials: readonly Credential[] | null;
  readonly hasOtherDefault: boolean;
  readonly onCreated: (message: string) => void;
}) {
  const blank: ProfileDraft = {
    name: "",
    providerSlug: providers[0] ?? "",
    modelId: "",
    parameters: "{}",
    credentialId: "",
    isDefault: false,
  };
  const [draft, setDraft] = useState<ProfileDraft>(blank);
  const [problem, setProblem] = useState<FormError | null>(null);
  const [pending, startTransition] = useTransition();

  function submit() {
    const built = createBody(draft, credentials);
    if (!built.ok) {
      setProblem({ message: built.message, field: built.field });
      return;
    }
    setProblem(null);
    startTransition(async () => {
      const result = await createModelProfileAction(workspaceId, built.body);
      if (result.ok) {
        // Cleared only on success, so a refusal leaves everything typed -
        // including the parameters JSON, which is the expensive field to lose.
        setDraft(blank);
        onCreated(`Added '${result.profile.name}'.`);
      } else {
        setProblem({ message: result.message, field: draftFieldFor(result.field) });
      }
    });
  }

  return (
    <form
      className="form"
      action={() => {
        submit();
      }}
    >
      <ProfileFields
        idPrefix="new-profile"
        draft={draft}
        setDraft={setDraft}
        providers={providers}
        credentials={credentials}
        hasOtherDefault={hasOtherDefault}
        problem={problem}
      />
      {problem !== null && problem.field === null && (
        <p className="form__error" role="alert">
          {problem.message}
        </p>
      )}
      <div className="form__actions">
        <button className="button" type="submit" disabled={pending}>
          {pending ? "Adding…" : "Add profile"}
        </button>
      </div>
    </form>
  );
}

/**
 * One profile's edit form, pre-filled with exactly what is stored.
 *
 * Its own state, so opening the editor on another row cannot inherit this
 * one's half-finished draft, and its own pending flag.
 */
function ProfileEditor({
  workspaceId,
  profile,
  providers,
  credentials,
  hasOtherDefault,
  onDone,
}: {
  readonly workspaceId: string;
  readonly profile: ModelProfile;
  readonly providers: readonly string[] | null;
  readonly credentials: readonly Credential[] | null;
  readonly hasOtherDefault: boolean;
  readonly onDone: (message: string) => void;
}) {
  const [draft, setDraft] = useState<ProfileDraft>(draftOf(profile));
  const [problem, setProblem] = useState<FormError | null>(null);
  const [pending, startTransition] = useTransition();

  function submit() {
    const built = profileEdits(profile, draft, credentials);
    if (!built.ok) {
      setProblem({ message: built.message, field: built.field });
      return;
    }
    if (!built.changed) {
      setProblem({ message: "Nothing has changed yet", field: null });
      return;
    }
    setProblem(null);
    const body = built.body;
    startTransition(async () => {
      const result = await updateModelProfileAction(workspaceId, profile.id, body);
      if (result.ok) onDone(`Saved '${result.profile.name}'.`);
      else setProblem({ message: result.message, field: draftFieldFor(result.field) });
    });
  }

  return (
    <form
      className="form"
      action={() => {
        submit();
      }}
    >
      <ProfileFields
        idPrefix={`profile-${profile.id}`}
        draft={draft}
        setDraft={setDraft}
        // The stored slug is kept as a choice even when this build no longer
        // serves it: the API deliberately lets such a row be renamed and
        // detached, and a select that could not show its own value would turn
        // a rename into a provider migration.
        providers={providerChoices(providers ?? [profile.provider_slug], profile.provider_slug)}
        credentials={credentials}
        hasOtherDefault={hasOtherDefault}
        problem={problem}
        storedCredentialId={profile.credential_id}
      />
      {problem !== null && problem.field === null && (
        <p className="form__error" role="alert">
          {problem.message}
        </p>
      )}
      <div className="form__actions">
        <button className="button" type="submit" disabled={pending}>
          {pending ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

/**
 * The six fields, shared by both forms so they cannot disagree.
 *
 * Controlled inputs throughout, which is what makes a refusal survivable: a
 * form whose action returns a failure keeps every value, rather than handing
 * back an empty parameters box after a 422.
 */
function ProfileFields({
  idPrefix,
  draft,
  setDraft,
  providers,
  credentials,
  hasOtherDefault,
  problem,
  storedCredentialId = null,
}: {
  readonly idPrefix: string;
  readonly draft: ProfileDraft;
  readonly setDraft: (draft: ProfileDraft) => void;
  readonly providers: readonly string[];
  readonly credentials: readonly Credential[] | null;
  readonly hasOtherDefault: boolean;
  readonly problem: FormError | null;
  /**
   * The credential this profile already holds, if any.
   *
   * Offered as a choice even when it is not in the list - a deleted
   * credential's row survives as a tombstone that no listing returns, and a
   * profile can legitimately still point at it. Without this option the
   * select would show the wrong value and saving a rename would silently
   * repoint the key.
   */
  readonly storedCredentialId?: string | null;
}) {
  const availability = credentialAvailability(credentials, draft.providerSlug);
  const offered = availability.state === "available" ? availability.credentials : [];
  const storedIsOffered =
    storedCredentialId === null || offered.some((entry) => entry.id === storedCredentialId);

  function fieldError(field: keyof ProfileDraft): string | null {
    return problem !== null && problem.field === field ? problem.message : null;
  }

  return (
    <>
      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-name`}>
          Name
        </label>
        <input
          className="form__input"
          id={`${idPrefix}-name`}
          maxLength={200}
          placeholder="Production writer"
          value={draft.name}
          onChange={(event) => {
            setDraft({ ...draft, name: event.target.value });
          }}
        />
        {fieldError("name") === null ? (
          <p className="form__hint">
            How an agent&apos;s configuration will refer to it. Unique within the
            workspace.
          </p>
        ) : (
          <p className="form__error" role="alert">
            {fieldError("name")}
          </p>
        )}
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-provider`}>
          Provider
        </label>
        <select
          className="form__input"
          id={`${idPrefix}-provider`}
          value={draft.providerSlug}
          onChange={(event) => {
            const providerSlug = event.target.value;
            setDraft({
              ...draft,
              providerSlug,
              // A provider change re-checks the pair on the API's side, so an
              // incompatible credential is dropped here instead of becoming a
              // 422 about a field this form did not touch.
              credentialId: credentialAfterProviderChange(
                draft.credentialId,
                providerSlug,
                credentials,
              ),
            });
          }}
        >
          {providers.map((slug) => (
            <option key={slug} value={slug}>
              {slug}
            </option>
          ))}
        </select>
        {fieldError("providerSlug") === null ? (
          <p className="form__hint">
            What this build can serve, from <code>/v1/meta</code>. Anything else is
            refused, so these are the whole of the choice.
          </p>
        ) : (
          <p className="form__error" role="alert">
            {fieldError("providerSlug")}
          </p>
        )}
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-model`}>
          Model
        </label>
        <input
          className="form__input"
          id={`${idPrefix}-model`}
          maxLength={200}
          placeholder="claude-opus-5"
          value={draft.modelId}
          onChange={(event) => {
            setDraft({ ...draft, modelId: event.target.value });
          }}
        />
        {fieldError("modelId") === null ? (
          <p className="form__hint">
            The model id as the provider spells it. AgentWorld passes it through and does
            not keep a list of them.
          </p>
        ) : (
          <p className="form__error" role="alert">
            {fieldError("modelId")}
          </p>
        )}
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-credential`}>
          Credential
        </label>
        <select
          className="form__input"
          id={`${idPrefix}-credential`}
          value={draft.credentialId}
          onChange={(event) => {
            setDraft({ ...draft, credentialId: event.target.value });
          }}
        >
          <option value="">No credential</option>
          {!storedIsOffered && storedCredentialId !== null && (
            <option value={storedCredentialId}>
              Keep the current credential (not in the list)
            </option>
          )}
          {offered.map((credential) => (
            <option key={credential.id} value={credential.id}>
              {credentialLabel(credential)}
            </option>
          ))}
        </select>
        {fieldError("credentialId") === null ? (
          <p className="form__hint">{availabilityHint(availability)}</p>
        ) : (
          <p className="form__error" role="alert">
            {fieldError("credentialId")}
          </p>
        )}
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-parameters`}>
          Parameters
        </label>
        <textarea
          className="form__input"
          id={`${idPrefix}-parameters`}
          rows={5}
          spellCheck={false}
          value={draft.parameters}
          onChange={(event) => {
            setDraft({ ...draft, parameters: event.target.value });
          }}
        />
        {fieldError("parameters") === null ? (
          <p className="form__hint">
            A JSON object, stored as written. Two keys are read when a step runs -{" "}
            <code>base_url</code> and <code>max_output_tokens</code> - and everything else
            is kept untouched for whatever reads it next, which is why this is an object
            rather than a set of boxes.
          </p>
        ) : (
          <p className="form__error" role="alert">
            {fieldError("parameters")}
          </p>
        )}
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor={`${idPrefix}-default`}>
          Workspace default
        </label>
        <select
          className="form__input"
          id={`${idPrefix}-default`}
          value={draft.isDefault ? "yes" : "no"}
          onChange={(event) => {
            setDraft({ ...draft, isDefault: event.target.value === "yes" });
          }}
        >
          <option value="no">Not the default</option>
          <option value="yes">The workspace default</option>
        </select>
        <p className="form__hint">{defaultHint(draft.isDefault, hasOtherDefault)}</p>
      </div>
    </>
  );
}
