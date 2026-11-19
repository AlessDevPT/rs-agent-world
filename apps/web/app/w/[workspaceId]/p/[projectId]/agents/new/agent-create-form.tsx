"use client";

import Link from "next/link";
import { useState, useTransition } from "react";

import { createAgentAction } from "@/app/actions";
import {
  EMPTY_DRAFT,
  heartbeatNote,
  instructionsHint,
  keyPreview,
  labelsHint,
  modeChoices,
  profileAfterModeChange,
  profileChoices,
  profileWarning,
  profilesEmptyState,
  createBody,
  type AgentDraft,
} from "@/lib/agents/create";
import type { ModelProfile } from "@/lib/api/model-profiles";

interface Problem {
  readonly message: string;
  readonly field?: string | undefined;
  readonly requestId?: string | undefined;
}

/**
 * The form that registers an agent into a project.
 *
 * **Every decision it makes lives in `lib/agents/create.ts`**, which is where
 * the tests are: what key the API will derive, which fields are omitted when
 * blank, whether a model profile may travel, and which input a refusal
 * belongs to. This component holds the inputs and the pending flag and
 * nothing else worth testing - vitest here is node-only, so a component is
 * unreachable from a test and anything decided in one is decided untested.
 *
 * **Controlled inputs, deliberately.** A refusal arrives from the server
 * action and has to leave the typed text exactly where it was: somebody who
 * spent two minutes on an instructions block must not lose it because the key
 * was taken. `form` posts through `action={submit}`, so the browser's own
 * submit still works, and the values come from state rather than the DOM.
 *
 * The model profile picker is rendered only for an admin creating a managed
 * agent, which is the intersection of the API's two rules: `agent.configure`
 * is `admin`, and `_check_model_mode` refuses a model for any other mode.
 * That is a convenience and not a boundary - the endpoint checks both again.
 */
export function AgentCreateForm({
  workspaceId,
  projectId,
  profiles,
  mayConfigure,
}: {
  readonly workspaceId: string;
  readonly projectId: string;
  /**
   * The workspace's model profiles, or null when they could not be read.
   *
   * Null and `[]` are different sentences and are rendered as two: "we could
   * not ask" must never appear as "this workspace has none".
   */
  readonly profiles: readonly ModelProfile[] | null;
  readonly mayConfigure: boolean;
}) {
  const [draft, setDraft] = useState<AgentDraft>(EMPTY_DRAFT);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [pending, startTransition] = useTransition();

  const key = keyPreview(draft);
  const selected = (profiles ?? []).find((profile) => profile.id === draft.modelProfileId) ?? null;
  const warning = profileWarning(draft.mode, selected);
  const heartbeat = heartbeatNote(draft.mode);
  const empty = profilesEmptyState();

  function fieldError(field: string): string | null {
    return problem !== null && problem.field === field ? problem.message : null;
  }

  function submit() {
    setProblem(null);
    const outcome = createBody(draft, { mayConfigure });
    if (!outcome.ok) {
      // Refused before the request: the API would answer the same way, and
      // the slug rules in particular arrive from it with nothing pointing at
      // an input. Nothing is cleared.
      setProblem({ message: outcome.message, field: outcome.field });
      return;
    }

    startTransition(async () => {
      const result = await createAgentAction(workspaceId, projectId, outcome.body);
      // A success redirects to the new agent's page, so only a failure
      // returns anything at all.
      if (result !== undefined && !result.ok) {
        setProblem({
          message: result.message,
          field: result.field,
          requestId: result.requestId,
        });
      }
    });
  }

  return (
    <form className="form" action={submit}>
      {problem !== null && problem.field === undefined && (
        <p className="form__error" role="alert">
          {problem.message}
          {problem.requestId !== undefined && <> (request {problem.requestId})</>}
        </p>
      )}

      <div className="card">
        <h2 className="card__title">Identity</h2>

        <div className="form__row">
          <label className="form__label" htmlFor="display-name">
            Display name
          </label>
          <input
            className="form__input"
            id="display-name"
            maxLength={200}
            placeholder="Market Researcher"
            value={draft.displayName}
            onChange={(event) => {
              setDraft({ ...draft, displayName: event.target.value });
            }}
          />
          {fieldError("display_name") === null ? (
            <p className="form__hint">What this agent is called everywhere in AgentWorld.</p>
          ) : (
            <p className="form__error" role="alert">
              {fieldError("display_name")}
            </p>
          )}
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-key">
            Key <span className="muted">— optional</span>
          </label>
          <input
            className="form__input"
            id="agent-key"
            maxLength={48}
            placeholder={key.derived && key.key !== "" ? key.key : "market-researcher"}
            value={draft.agentKey}
            onChange={(event) => {
              setDraft({ ...draft, agentKey: event.target.value });
            }}
          />
          {fieldError("agent_key") === null ? (
            <p className="form__hint">
              The identifier a worker uses to identify itself, unique within this project
              and fixed once the agent exists.{" "}
              {key.key === "" ? (
                <>Derived from the display name when left blank.</>
              ) : (
                <>
                  {key.derived ? "Will be derived as" : "Will be stored as"}{" "}
                  <code>{key.key}</code>.
                </>
              )}
            </p>
          ) : (
            <p className="form__error" role="alert">
              {fieldError("agent_key")}
            </p>
          )}
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-role">
            Role <span className="muted">— optional</span>
          </label>
          <input
            className="form__input"
            id="agent-role"
            placeholder="Researcher"
            value={draft.role}
            onChange={(event) => {
              setDraft({ ...draft, role: event.target.value });
            }}
          />
          <p className="form__hint">
            A short label for what this agent is for. Descriptive only — nothing in
            AgentWorld behaves differently because of it.
          </p>
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-kind">
            Kind <span className="muted">— optional</span>
          </label>
          <input
            className="form__input"
            id="agent-kind"
            placeholder="researcher"
            value={draft.kind}
            onChange={(event) => {
              setDraft({ ...draft, kind: event.target.value });
            }}
          />
          <p className="form__hint">
            Your own classification, for your own tooling. Also descriptive only.
          </p>
        </div>
      </div>

      <div className="card">
        <h2 className="card__title">How AgentWorld works with it</h2>
        <p className="muted">
          This decides what may be asked of the agent. The edit screen does not change it,
          so it is worth reading the three.
        </p>

        {modeChoices().map((choice) => (
          <div className="form__row" key={choice.mode}>
            <label className="form__label" htmlFor={`mode-${choice.mode}`}>
              <input
                checked={draft.mode === choice.mode}
                id={`mode-${choice.mode}`}
                name="interaction-mode"
                type="radio"
                value={choice.mode}
                onChange={() => {
                  const mode = choice.mode;
                  setDraft({
                    ...draft,
                    mode,
                    // Only a managed agent may carry a model, so the
                    // selection is dropped here rather than becoming a 422
                    // about a field the person has stopped looking at.
                    modelProfileId: profileAfterModeChange(draft.modelProfileId, mode),
                  });
                }}
              />{" "}
              {choice.label}
            </label>
            <p className="form__hint">{choice.detail}</p>
          </div>
        ))}

        {heartbeat !== null && <p className="form__hint">{heartbeat}</p>}
      </div>

      <div className="card">
        <h2 className="card__title">What it is told</h2>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-description">
            Description <span className="muted">— optional</span>
          </label>
          <textarea
            className="form__input"
            id="agent-description"
            rows={2}
            value={draft.description}
            onChange={(event) => {
              setDraft({ ...draft, description: event.target.value });
            }}
          />
          <p className="form__hint">
            For the people reading this project, not for the model.
          </p>
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-instructions">
            Instructions <span className="muted">— optional</span>
          </label>
          <textarea
            className="form__input"
            id="agent-instructions"
            rows={6}
            value={draft.instructions}
            onChange={(event) => {
              setDraft({ ...draft, instructions: event.target.value });
            }}
          />
          <p className="form__hint">{instructionsHint(draft.mode)}</p>
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-labels">
            Labels <span className="muted">— optional</span>
          </label>
          <textarea
            className="form__input"
            id="agent-labels"
            rows={3}
            spellCheck={false}
            value={draft.labels}
            onChange={(event) => {
              setDraft({ ...draft, labels: event.target.value });
            }}
          />
          {fieldError("labels") === null ? (
            <p className="form__hint">{labelsHint()}</p>
          ) : (
            <p className="form__error" role="alert">
              {fieldError("labels")}
            </p>
          )}
        </div>
      </div>

      {mayConfigure && draft.mode === "managed" && (
        <div className="card">
          <h2 className="card__title">Model</h2>

          {profiles === null ? (
            <p className="form__hint">
              The workspace&apos;s model profiles could not be read, so none can be offered
              here. You can create the agent now and attach one from{" "}
              <Link href={`/w/${workspaceId}/settings/model-profiles`}>model profiles</Link>.
            </p>
          ) : profiles.length === 0 ? (
            <>
              <p className="muted">{empty.headline}</p>
              <p className="form__hint">
                {empty.detail}{" "}
                <Link href={`/w/${workspaceId}/settings/model-profiles`}>
                  Create a model profile
                </Link>
                .
              </p>
            </>
          ) : (
            <div className="form__row">
              <label className="form__label" htmlFor="model-profile">
                Model profile
              </label>
              <select
                className="form__input"
                id="model-profile"
                value={draft.modelProfileId}
                onChange={(event) => {
                  setDraft({ ...draft, modelProfileId: event.target.value });
                }}
              >
                <option value="">No model profile</option>
                {profileChoices(profiles).map((choice) => (
                  <option key={choice.id} value={choice.id}>
                    {choice.label}
                  </option>
                ))}
              </select>
              {fieldError("model_profile_id") === null ? (
                <p className="form__hint">
                  The provider, model and credential this agent calls with. Workspace-wide,
                  and managed by an admin in{" "}
                  <Link href={`/w/${workspaceId}/settings/model-profiles`}>
                    model profiles
                  </Link>
                  .
                </p>
              ) : (
                <p className="form__error" role="alert">
                  {fieldError("model_profile_id")}
                </p>
              )}
            </div>
          )}

          {warning !== null && <p className="form__hint">{warning}</p>}
        </div>
      )}

      <div className="form__actions">
        <button className="button button--primary" disabled={pending} type="submit">
          {pending ? "Creating…" : "Create agent"}
        </button>
        <Link className="button button--quiet" href={`/w/${workspaceId}/p/${projectId}/agents`}>
          Cancel
        </Link>
      </div>
    </form>
  );
}
