"use client";

import Link from "next/link";
import { unstable_rethrow, useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition, type Ref } from "react";

import { testAgentAction } from "@/app/actions";
import { SAMPLE_MAX, testOutcomeNote } from "@/lib/agents/test";
import {
  INITIAL_PANEL_STATE,
  beginTest,
  createPanelStore,
  finishTest,
  panelAcceptsInput,
  panelBusy,
  testPanelView,
  type PanelState,
  type PanelStore,
} from "@/lib/agents/test-panel";
import type { AgentInteractionMode } from "@/lib/api/agents";
import type { MemberRole } from "@/lib/api/tenancy";

/**
 * Run this agent once, against a sample you type.
 *
 * **Every decision it makes lives in `lib/agents/test-panel.ts` and
 * `lib/agents/test.ts`** - who sees a form, what each refusal means, what the
 * warning says, and what a second click does. This component holds the
 * textarea, some refs and the markup, for the reason its siblings give:
 * vitest here is node-only, so a decision taken inside a component is a
 * decision nothing checks.
 *
 * **Three scalars, not the agent.** The props are `interactionMode`,
 * `archivedAt` and `hasModelProfile`, and the narrowing is not tidiness: a
 * client component's props are serialised into the page's flight payload
 * whether it reads them or not, and `AgentView` carries `instructions`,
 * `labels`, `model_overrides` and `dispatch` - the four fields the detail
 * page deliberately does not render, because a deployment may have put an
 * endpoint or a token in any of them. A `Pick<Agent, ...>` would type that
 * away and ship it anyway.
 *
 * **It spends money, and it says so before it does.** A test is a real
 * execution against the agent's configured provider and credential. So the
 * first button does not start one: it swaps itself for the warning and two
 * deliberate buttons, which is the same confirm step the credential and
 * model-profile managers use before anything irreversible.
 *
 * **One authoritative state, in `createPanelStore`.** The `useState` here is
 * a copy for rendering; the store is what every decision reads and every
 * event changes, and it is current synchronously. The two used to be
 * separate - a render-captured snapshot for the decision, a functional
 * `setState` for the change - and a confirmation that a cancel had already
 * overtaken could send a charged request whose outcome was then discarded.
 *
 * **Two guards against a second request, because one is not enough.**
 * `disabled` only takes effect on the next render, and a double-click lands
 * before it. `inFlight` is a ref, checked synchronously at the top of the
 * handler, and it is what actually stops the second call; the state machine
 * stops everything after that. This matters more here than anywhere else in
 * the app: the endpoint has no idempotency key and no rate limit, so two
 * requests are two executions and two provider charges.
 *
 * **The sequence itself is `beginTest` and `finishTest`, in that same module,
 * and not here.**
 * A server action can reject rather than return - a dropped connection, a
 * stale action id after a deploy, a 500 from the Next server - and the
 * `finally` that releases the guard afterwards is the line the panel's
 * recovery depends on. A `finally` written in this file would be a line no
 * test in this project can reach, so it is written where one can. What is
 * passed in from here is only what belongs to React and to Next: the
 * request, the router refresh, and `unstable_rethrow` for the rejection
 * Next uses to redirect.
 *
 * **202 is an acceptance.** The success state says *started*, never *passed*:
 * the execution exists and the agent has not answered. Nothing on this site
 * polls, so it points at the execution and says to reload it rather than
 * implying something here is watching.
 */
export function AgentTestPanel({
  workspaceId,
  projectId,
  agentId,
  interactionMode,
  archivedAt,
  hasModelProfile,
  role,
}: {
  readonly workspaceId: string;
  readonly projectId: string;
  readonly agentId: string;
  readonly interactionMode: AgentInteractionMode;
  readonly archivedAt: string | null;
  readonly hasModelProfile: boolean;
  readonly role: MemberRole;
}) {
  const router = useRouter();
  const [sample, setSample] = useState("");
  const [state, setState] = useState<PanelState>(INITIAL_PANEL_STATE);
  const [, startTransition] = useTransition();
  // The guard that works inside one render. See the note above.
  const inFlight = useRef(false);

  // **The one place this panel's state lives.** `state` above is a copy kept
  // for rendering; every decision and every transition goes through the
  // store, which is current the instant it is sent to rather than at the
  // next render. Built once, lazily: the ref survives re-renders, and
  // `setState` is stable, so the store always publishes to a live setter.
  const storeRef = useRef<PanelStore | null>(null);
  const store = (storeRef.current ??= createPanelStore(setState));

  const confirmRef = useRef<HTMLParagraphElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const blockedRef = useRef<HTMLParagraphElement>(null);
  const lastStep = useRef<PanelState["step"] | null>(null);

  const view = testPanelView({ interactionMode, archivedAt, hasModelProfile }, role);
  const busy = panelBusy(state);

  /**
   * Focus follows the step, because every step of this panel removes the
   * control that was focused when it began.
   *
   * Pressing *Run a test* destroys that button; confirming or cancelling
   * destroys the one that was pressed; a refusal replaces both. Each one
   * would otherwise drop a keyboard user on `<body>` with a decision waiting
   * for them somewhere above. The target is the thing that explains the new
   * step - the confirmation, the error, the notice - except when the panel is
   * back at rest, where it is the button that starts the sequence again.
   *
   * Two deliberate omissions. Nothing is focused on the first render: this is
   * one card among several and has no claim on the page until somebody has
   * used it. And nothing is focused when the step has not changed, so typing
   * in the sample box never pulls focus out of it.
   */
  useEffect(() => {
    const from = lastStep.current;
    lastStep.current = state.step;
    if (from === null || from === state.step) return;

    const target =
      state.step === "confirming" || state.step === "pending"
        ? confirmRef.current
        : state.step === "refused"
          ? errorRef.current
          : state.step === "blocked"
            ? blockedRef.current
            : // `idle` after a cancel, and `accepted`. The outcome is
              // announced by its own live region rather than being focused,
              // which is what it is for; focus goes to the control that
              // replaced the one the person just used.
              triggerRef.current;
    target?.focus();
  }, [state.step]);

  function confirm() {
    // Outside the transition, deliberately: this is the update that puts
    // "Starting…" on the screen and disables the controls, and an update
    // dispatched inside a transition is not urgent - React holds it until
    // the action settles, which is exactly long enough for it to be useless.
    if (!beginTest({ guard: inFlight, store })) return;

    startTransition(async () => {
      await finishTest({
        guard: inFlight,
        store,
        // Next rejects a server action's promise when it redirects, which is
        // how an expired session reaches sign-in. This hands that error back
        // out instead of reading it as an uncertain failure - and out of a
        // transition, which is where the siblings leave theirs too.
        rethrow: unstable_rethrow,
        onAccepted: () => {
          // The action revalidated this route; this asks for the new server
          // render, so the execution that was just started appears in the
          // Executions card below without a manual reload.
          router.refresh();
        },
        run: async () => {
          const result = await testAgentAction(workspaceId, projectId, agentId, sample);
          return result.ok
            ? { type: "accepted", executionId: result.execution.id }
            : { type: "refused", refusal: result };
        },
      });
    });
  }

  if (view.kind === "notice") {
    return (
      <div className="card">
        <h2 className="card__title">Test this agent</h2>
        <Notice headline={view.notice.headline} detail={view.notice.detail} />
      </div>
    );
  }

  const note = testOutcomeNote();
  const fieldError = state.step === "refused" && state.field === "input" ? state.message : null;
  const generalError = state.step === "refused" && state.field !== "input" ? state : null;
  const hintIds = ["test-sample-hint", fieldError !== null ? "test-sample-error" : null]
    .filter((id): id is string => id !== null)
    .join(" ");
  // One branch for both halves of the decision, so the controls keep their
  // place - and their focus - when the request goes out. Splitting them is
  // what made `busy` unreachable here: a step cannot be `confirming` and
  // `pending` at once, so a "Starting…" label inside a confirming-only
  // branch was a label nothing could ever render.
  const deciding = state.step === "confirming" || state.step === "pending";

  return (
    <div aria-busy={busy} className="card">
      <h2 className="card__title">Test this agent</h2>

      {state.step === "blocked" ? (
        <Notice
          detail={state.notice.detail}
          detailRef={blockedRef}
          headline={state.notice.headline}
        />
      ) : (
        <>
          <p className="muted">
            <strong>{view.warning.headline}.</strong> {view.warning.detail}
          </p>

          {view.profileWarning !== null && (
            <p className="form__hint">{view.profileWarning}</p>
          )}

          {generalError !== null && (
            <p className="form__error" ref={errorRef} role="alert" tabIndex={-1}>
              {generalError.message}
              {generalError.requestId !== null && <> (request {generalError.requestId})</>}
            </p>
          )}

          {state.step === "accepted" && (
            <p className="form__hint" role="status">
              <strong>{note.headline}.</strong> {note.detail}{" "}
              <Link href={`/w/${workspaceId}/p/${projectId}/executions/${state.executionId}`}>
                Open the execution
              </Link>
            </p>
          )}

          <div className="form__row">
            <label className="form__label" htmlFor="test-sample">
              Sample input
            </label>
            <textarea
              aria-describedby={hintIds}
              aria-invalid={fieldError !== null}
              className="form__input"
              disabled={busy || !panelAcceptsInput(state)}
              id="test-sample"
              rows={4}
              value={sample}
              onChange={(event) => {
                setSample(event.target.value);
              }}
            />
            <p className="form__hint" id="test-sample-hint">
              What the agent is asked, as its one message. Its own instructions are sent as
              well. {countLabel(sample)}
            </p>
            {fieldError !== null && (
              <p
                className="form__error"
                id="test-sample-error"
                ref={errorRef}
                role="alert"
                tabIndex={-1}
              >
                {fieldError}
              </p>
            )}
          </div>

          {deciding ? (
            <>
              {/* The alert carries the sentence; the buttons sit outside it,
                  because a control inside a live region is announced
                  unpredictably. `tabIndex={-1}` makes it focusable for the
                  effect above without putting it in the tab order. It is one
                  element across both halves of the decision on purpose: the
                  person it was focused for keeps their place when the
                  request goes out. */}
              <p className="form__error" ref={confirmRef} role="alert" tabIndex={-1}>
                {busy
                  ? "Starting the run. This does not wait for the agent to answer."
                  : "This starts a real run against the configured provider now."}
              </p>
              <div className="form__actions">
                <button
                  className="button button--primary"
                  disabled={busy}
                  type="button"
                  onClick={confirm}
                >
                  {busy ? "Starting…" : "Yes, run the test"}
                </button>
                <button
                  className="button button--quiet"
                  disabled={busy}
                  type="button"
                  onClick={() => {
                    store.send({ type: "cancel" });
                  }}
                >
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <div className="form__actions">
              <button
                className="button button--quiet"
                ref={triggerRef}
                type="button"
                onClick={() => {
                  store.send({ type: "submit" });
                }}
              >
                {state.step === "accepted" ? "Run another test" : "Run a test"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Notice({
  headline,
  detail,
  detailRef,
}: {
  readonly headline: string | null;
  readonly detail: string;
  /** Present only where the notice replaced something that had focus. */
  readonly detailRef?: Ref<HTMLParagraphElement>;
}) {
  return (
    <>
      {headline !== null && <p>{headline}</p>}
      <p className="muted" ref={detailRef} tabIndex={-1}>
        {detail}
      </p>
    </>
  );
}

/**
 * The live count, which exists because the textarea has no `maxLength`.
 *
 * The DOM attribute counts UTF-16 units and truncates a paste silently -
 * which would both disagree with the API, whose `max_length` counts code
 * points, and change what the model is asked. So the limit is reported and
 * `sampleProblem` enforces it, rather than the browser quietly editing the
 * sample on the way in. Counted the same way the rule counts.
 */
function countLabel(sample: string): string {
  const used = [...sample].length;
  return used === 0
    ? `Up to ${String(SAMPLE_MAX)} characters.`
    : `${String(used)} of ${String(SAMPLE_MAX)} characters.`;
}
