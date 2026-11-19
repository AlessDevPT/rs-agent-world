"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { updateAgentAction } from "@/app/actions";
import {
  instructionsHint,
  labelsHint,
  profileChoices,
  profileWarning,
  profilesEmptyState,
} from "@/lib/agents/create";
import {
  draftOf,
  editBody,
  heartbeatHint,
  mayHaveModel,
  type EditDraft,
} from "@/lib/agents/edit";
import type { Agent } from "@/lib/api/agents";
import type { ModelProfile } from "@/lib/api/model-profiles";

interface Problem {
  readonly message: string;
  readonly field?: string | undefined;
  readonly requestId?: string | undefined;
}

/**
 * The form that changes an agent.
 *
 * **Every decision it makes lives in `lib/agents/edit.ts`**, the way the
 * creation form's live in `create.ts`: what counts as changed, what a blank
 * box means, which fields an admin may send, and what the heartbeat limit
 * does. This component holds the inputs, the pending flag and the error it
 * was handed - vitest here is node-only, so anything decided in a component
 * is decided untested.
 *
 * **Controlled inputs, and nothing is cleared on a refusal.** Somebody who
 * rewrote an instructions block must not lose it because the model profile
 * was deleted underneath them, so the draft is state and a failed save
 * leaves it exactly as typed.
 *
 * **It opens as the stored agent and sends only what moved.** A PATCH that
 * re-sent every field would write an `agent.update` audit row naming them
 * all, which is a record of an edit that did not happen. `editBody` is where
 * that diff is made.
 *
 * The model picker is drawn only for an admin editing a managed agent - the
 * intersection of the API's two rules, `agent.configure` being `admin` and
 * `_check_model_mode` refusing a model in any other mode. The heartbeat
 * limit is drawn for an admin whatever the mode, because every agent has
 * one. Neither gate is the boundary.
 */
export function AgentEditForm({
  workspaceId,
  projectId,
  agent,
  profiles,
  mayConfigure,
}: {
  readonly workspaceId: string;
  readonly projectId: string;
  readonly agent: Agent;
  /**
   * The workspace's model profiles, or null when they could not be read -
   * which is also what a member gets, since they may not send one.
   */
  readonly profiles: readonly ModelProfile[] | null;
  readonly mayConfigure: boolean;
}) {
  const router = useRouter();
  // What the draft is diffed against, which is not the `agent` prop after
  // the first save: the prop is a server render away, and until
  // `router.refresh()` lands a second save would diff against what the
  // agent was rather than what it is - and re-send the first save's fields.
  // The action hands back the agent it wrote, so that is the baseline.
  const [baseline, setBaseline] = useState<Agent>(agent);
  const [draft, setDraft] = useState<EditDraft>(() => draftOf(agent));
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState(false);
  const [pending, startTransition] = useTransition();

  const detail = `/w/${workspaceId}/p/${projectId}/agents/${agent.id}`;
  const offersModel = mayConfigure && mayHaveModel(agent);
  const selected = (profiles ?? []).find((profile) => profile.id === draft.modelProfileId) ?? null;
  const warning = offersModel ? profileWarning(agent.interaction_mode, selected) : null;
  const empty = profilesEmptyState();

  function fieldError(field: string): string | null {
    return problem !== null && problem.field === field ? problem.message : null;
  }

  function change(patch: Partial<EditDraft>) {
    setSaved(false);
    setDraft({ ...draft, ...patch });
  }

  function submit() {
    setProblem(null);
    setSaved(false);

    const outcome = editBody(baseline, draft, { mayConfigure });
    if (!outcome.ok) {
      // Refused before the request. "Nothing has changed yet" is one of
      // these: the API would accept that PATCH and record an audit row for
      // an edit that wrote no column.
      setProblem({ message: outcome.message, field: outcome.field });
      return;
    }

    startTransition(async () => {
      const result = await updateAgentAction(workspaceId, projectId, agent.id, outcome.body);
      if (result.ok) {
        // The server action revalidated this route and the list; this asks
        // the router for the new server render, so the heading, the pills
        // and the detail page behind it agree with what was just saved.
        setBaseline(result.agent);
        setSaved(true);
        router.refresh();
        return;
      }
      setProblem({
        message: result.message,
        // The API's own locator, in the API's spelling, which is what the
        // inputs below are keyed on. Nothing is guessed from the status:
        // this endpoint names the field whenever it has one to name.
        field: result.field,
        requestId: result.requestId,
      });
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

      {saved && problem === null && (
        <p className="form__hint" role="status">
          Saved.
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
            value={draft.displayName}
            onChange={(event) => {
              change({ displayName: event.target.value });
            }}
          />
          {fieldError("display_name") === null ? (
            <p className="form__hint">
              What this agent is called everywhere in AgentWorld. It cannot be emptied.
            </p>
          ) : (
            <p className="form__error" role="alert">
              {fieldError("display_name")}
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
            value={draft.role}
            onChange={(event) => {
              change({ role: event.target.value });
            }}
          />
          <p className="form__hint">
            A short label for what this agent is for. Descriptive only. Empty it to clear
            it.
          </p>
        </div>

        <div className="form__row">
          <label className="form__label" htmlFor="agent-kind">
            Kind <span className="muted">— optional</span>
          </label>
          <input
            className="form__input"
            id="agent-kind"
            value={draft.kind}
            onChange={(event) => {
              change({ kind: event.target.value });
            }}
          />
          <p className="form__hint">
            Your own classification, for your own tooling. Also descriptive only.
          </p>
        </div>
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
              change({ description: event.target.value });
            }}
          />
          <p className="form__hint">For the people reading this project, not for the model.</p>
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
              change({ instructions: event.target.value });
            }}
          />
          <p className="form__hint">{instructionsHint(agent.interaction_mode)}</p>
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
              change({ labels: event.target.value });
            }}
          />
          {fieldError("labels") === null ? (
            <p className="form__hint">{labelsHint()} Empty the box for none.</p>
          ) : (
            <p className="form__error" role="alert">
              {fieldError("labels")}
            </p>
          )}
        </div>
      </div>

      {mayConfigure && (
        <div className="card">
          <h2 className="card__title">Configuration</h2>
          <p className="muted">
            These two need the admin role. The API refuses the whole request from anyone
            else, so a member&apos;s form does not offer them.
          </p>

          {offersModel &&
            (profiles === null ? (
              <p className="form__hint">
                The workspace&apos;s model profiles could not be read, so none can be
                offered here. The agent keeps the one it has.{" "}
                <Link href={`/w/${workspaceId}/settings/model-profiles`}>Model profiles</Link>
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
                    change({ modelProfileId: event.target.value });
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
                    and managed in{" "}
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
            ))}

          {warning !== null && <p className="form__hint">{warning}</p>}

          <div className="form__row">
            <label className="form__label" htmlFor="heartbeat-ttl">
              Heartbeat limit <span className="muted">— seconds</span>
            </label>
            <input
              className="form__input"
              id="heartbeat-ttl"
              inputMode="numeric"
              value={draft.heartbeatTtl}
              onChange={(event) => {
                change({ heartbeatTtl: event.target.value });
              }}
            />
            {fieldError("heartbeat_ttl_seconds") === null ? (
              <p className="form__hint">{heartbeatHint(agent)}</p>
            ) : (
              <p className="form__error" role="alert">
                {fieldError("heartbeat_ttl_seconds")}
              </p>
            )}
          </div>
        </div>
      )}

      <div className="form__actions">
        <button className="button button--primary" disabled={pending} type="submit">
          {pending ? "Saving…" : "Save changes"}
        </button>
        <Link className="button button--quiet" href={detail}>
          {pending ? "Cancel" : "Back to the agent"}
        </Link>
      </div>
    </form>
  );
}
