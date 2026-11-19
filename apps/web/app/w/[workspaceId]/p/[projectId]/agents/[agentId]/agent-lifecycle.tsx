"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { archiveAgentAction, restoreAgentAction } from "@/app/actions";
import { archiveWarning, restoreNote } from "@/lib/agents/edit";

/**
 * What either call answers with.
 *
 * Taken from the actions themselves rather than restated, so that a change
 * to `ActionFailure` - the `status` it gained for exactly these two - cannot
 * leave this component reading a shape that no longer arrives.
 */
type LifecycleOutcome =
  | Awaited<ReturnType<typeof archiveAgentAction>>
  | Awaited<ReturnType<typeof restoreAgentAction>>;

/**
 * Archive and restore, for an admin looking at one agent.
 *
 * A client component for the reason the model-profile manager is one: a
 * confirm step and a pending flag. The sentences are not here - what
 * archiving costs is `archiveWarning` in `lib/agents/edit.ts`, where a test
 * can read it, because that copy is the whole of what a person has to decide
 * on and it must not drift from what the backend does.
 *
 * **Confirmation before archiving, none before restoring.** The same
 * asymmetry the credential and model-profile managers have: the step is
 * there for the action whose effect reaches other things - workflows naming
 * this agent begin to fail - and restoring only undoes it. Both are
 * reversible, which is why neither says "cannot be undone"; archiving is
 * confirmed because of what it interrupts, not because it is permanent.
 *
 * **A failure is shown and nothing else happens.** The 409 for an agent
 * somebody else archived a moment ago arrives as a refusal like any other,
 * and is reported rather than smoothed into a success - this page would
 * otherwise claim to have done something it did not do. The server action
 * revalidates on success only; `router.refresh()` then asks for the render
 * that reflects it.
 */
export function AgentLifecycle({
  workspaceId,
  projectId,
  agentId,
  agentKey,
  archived,
}: {
  readonly workspaceId: string;
  readonly projectId: string;
  readonly agentId: string;
  readonly agentKey: string;
  readonly archived: boolean;
}) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState<string | undefined>(undefined);
  const [pending, startTransition] = useTransition();

  const warning = archiveWarning(agentKey);

  function run(call: () => Promise<LifecycleOutcome>) {
    setError(null);
    setRequestId(undefined);
    startTransition(async () => {
      const result = await call();
      if (result.ok) {
        setConfirming(false);
        router.refresh();
        return;
      }
      setError(result.message);
      setRequestId(result.requestId);
    });
  }

  return (
    <div className="card">
      <h2 className="card__title">{archived ? "Restore" : "Archive"}</h2>

      {error !== null && (
        <p className="form__error" role="alert">
          {error}
          {requestId !== undefined && <> (request {requestId})</>}
        </p>
      )}

      {archived ? (
        <>
          <p className="muted">{restoreNote()}</p>
          <p>
            <button
              className="button button--primary"
              disabled={pending}
              type="button"
              onClick={() => {
                run(() => restoreAgentAction(workspaceId, projectId, agentId));
              }}
            >
              {pending ? "Restoring…" : "Restore this agent"}
            </button>
          </p>
        </>
      ) : (
        <>
          <p className="muted">
            An archived agent is left out of this project&apos;s agent list unless the
            list is asked for it, and cannot be edited or given work. It keeps its runs,
            its history and its executions, and you can restore it.
          </p>

          {confirming ? (
            <p className="form__error" role="alert">
              <strong>{warning.headline}.</strong> {warning.detail}{" "}
              <button
                className="button button--quiet"
                disabled={pending}
                type="button"
                onClick={() => {
                  run(() => archiveAgentAction(workspaceId, projectId, agentId));
                }}
              >
                {pending ? "Archiving…" : "Archive it"}
              </button>{" "}
              <button
                className="button button--quiet"
                disabled={pending}
                type="button"
                onClick={() => {
                  setConfirming(false);
                }}
              >
                Keep it
              </button>
            </p>
          ) : (
            <p>
              <button
                className="button button--quiet"
                disabled={pending}
                type="button"
                onClick={() => {
                  setError(null);
                  setConfirming(true);
                }}
              >
                Archive this agent
              </button>
            </p>
          )}
        </>
      )}
    </div>
  );
}
