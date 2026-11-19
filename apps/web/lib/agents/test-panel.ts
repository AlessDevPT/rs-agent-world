/**
 * What the agent test panel shows, and what pressing its buttons does.
 *
 * The second half of `test.ts`, split from it on a real seam: that module
 * holds the **rules** - who may test, which agents can be, what each refusal
 * means - and this one holds the **panel**, which is those rules arranged
 * into one view and one sequence of states. Stage A's module is imported,
 * never re-implemented; nothing here decides again what it already decided.
 *
 * It exists for the reason every `lib/agents/*` module exists: the Vitest
 * environment is `node`, there is no DOM, and no component in this codebase
 * is reachable from a test. So the panel's component holds inputs, a ref and
 * a pending flag, and every judgement it makes is one of the two functions
 * below - because a judgement inside the component is a judgement nothing
 * checks.
 *
 * **Why the state machine is here rather than being three `useState`s.** The
 * panel spends real money. "A second click must not start a second test" is
 * not a rendering detail, it is the behaviour most worth proving, and a
 * `disabled` attribute cannot be asserted in this test environment while a
 * reducer can. `panelStep` is total: every event is defined against every
 * state, and the ones that must do nothing return the state they were given
 * rather than falling through to a default nobody read.
 *
 * Nothing here renders, and nothing here calls the API.
 */

import type { Sentence } from "@/lib/agents/create";
import {
  agentTestability,
  canTestAgents,
  cannotTestNotice,
  testRefusal,
  testWarning,
} from "@/lib/agents/test";
import type { AgentInteractionMode } from "@/lib/api/agents";
import type { MemberRole } from "@/lib/api/tenancy";

/**
 * The whole of what the panel knows about the agent: three scalars.
 *
 * **Deliberately not `Pick<Agent, ...>`.** A `Pick` narrows the type and
 * nothing else - the object still carries every field at runtime, and a
 * client component's props are serialised into the page's flight payload
 * whether the component reads them or not. `AgentView` carries
 * `instructions`, `labels`, `model_overrides` and `dispatch`, which the agent
 * detail page deliberately does not render because a deployment may have put
 * an endpoint or a token in any of them. Handing the panel the whole agent
 * put all four into the page source. Three scalars cannot.
 *
 * `hasModelProfile` rather than the id for the same reason, one step smaller:
 * the panel only asks *whether* there is one, so the uuid has no business
 * crossing to the browser either.
 */
export interface PanelAgent {
  readonly interactionMode: AgentInteractionMode;
  readonly archivedAt: string | null;
  readonly hasModelProfile: boolean;
}

/**
 * Something to say instead of a form.
 *
 * `headline` is nullable because the two sources differ in shape and neither
 * is worth padding: a mode or archive refusal is a `Sentence` from
 * `testFindingSentence` and has one, while `cannotTestNotice` is a single
 * sentence that already explains itself - giving it an invented headline
 * would produce the "heartbeat current / Heartbeat is current" stutter the
 * detail page's own comments warn about.
 */
export interface PanelNotice {
  readonly headline: string | null;
  readonly detail: string;
}

export type PanelView =
  | {
      readonly kind: "form";
      /** Why this costs money. Stage A's, verbatim. */
      readonly warning: Sentence;
      /**
       * Said before the money is spent, when the agent has no model profile
       * to spend it through. Null when it has one.
       */
      readonly profileWarning: string | null;
    }
  | { readonly kind: "notice"; readonly notice: PanelNotice };

/**
 * The form, or the reason there is none.
 *
 * **Role first, then the agent.** A viewer looking at an `observed` agent is
 * told about their role, not about the mode: the API checks
 * `authorize(AGENT_TEST)` before it reads the agent at all, so the role is
 * the refusal they would actually receive, and leading with the mode would
 * promise that fixing the mode would help.
 *
 * Within the agent, the order is `agentTestability`'s, which is the
 * compiler's: archived, then observed, then dispatchable. An archived
 * `observed` agent reads as archived in both places.
 *
 * A convenience, not a boundary. `agent.test` is checked again on the
 * request, and Stage A's tests pin that a 403 comes back as a refusal rather
 * than being smoothed into a success.
 */
export function testPanelView(agent: PanelAgent, role: MemberRole): PanelView {
  if (!canTestAgents(role)) {
    return { kind: "notice", notice: { headline: null, detail: cannotTestNotice(role) } };
  }

  // Mapped back to the API's own spelling for Stage A's rule, which takes the
  // agent's shape. The two names for one fact live here, in one expression,
  // rather than in the component.
  const testability = agentTestability({
    interaction_mode: agent.interactionMode,
    archived_at: agent.archivedAt,
  });
  if (testability.kind === "refused") {
    return {
      kind: "notice",
      notice: { headline: testability.notice.headline, detail: testability.notice.detail },
    };
  }

  return {
    kind: "form",
    warning: testWarning(),
    profileWarning: agent.hasModelProfile ? null : missingProfileWarning(),
  };
}

/**
 * What to say before testing an agent that has no model profile.
 *
 * **"Probably", and deliberately not "will".** The dispatch resolves the
 * agent, its profile and the profile's credential when it performs the turn,
 * and a missing profile raises there - so the test is accepted, the
 * execution is created, and it fails as `misconfigured` without reaching a
 * provider. The *missing credential* half of that is covered by an
 * integration test; the missing-profile half is read off the runner and is
 * not, so this stops short of promising the outcome.
 *
 * It does not offer to fix it. Pointing at the model profile settings from
 * here would be guessing that the reader is an admin, which is what
 * `agent.configure` requires and what this panel does not know.
 */
export function missingProfileWarning(): string {
  return (
    "This agent has no model profile, and a turn takes its provider, model and " +
    "credential from one - so this test will probably fail before it reaches a provider. " +
    "It still starts, and the execution will say what happened."
  );
}

// ------------------------------------------------------------ the sequence ---

/**
 * What a refusal looks like to this module.
 *
 * Structural rather than `ActionFailure` imported from `@/app/actions`:
 * `lib/` does not depend on `app/` anywhere in this codebase, and the
 * action's result is assignable to this without being named. The fields are
 * the ones a panel renders or decides on.
 */
export interface PanelRefusal {
  readonly message: string;
  readonly codes?: readonly string[] | undefined;
  readonly field?: string | undefined;
  readonly requestId?: string | undefined;
}

export type PanelState =
  /** Nothing asked for yet. The sample may be typed and edited. */
  | { readonly step: "idle" }
  /** Asked for, not yet agreed to. The warning is on screen. */
  | { readonly step: "confirming" }
  /** The request is in flight. Every further submit is ignored. */
  | { readonly step: "pending" }
  /** 202. The execution exists; the agent has not answered. */
  | { readonly step: "accepted"; readonly executionId: string }
  /** Refused, and worth trying again once something changes. */
  | {
      readonly step: "refused";
      readonly message: string;
      readonly field: string | null;
      readonly requestId: string | null;
    }
  /** Refused for a reason no retry fixes: the mode, or the archive. */
  | { readonly step: "blocked"; readonly notice: PanelNotice };

export type PanelEvent =
  /** The person asked to run a test. */
  | { readonly type: "submit" }
  /** They backed out of the confirmation. */
  | { readonly type: "cancel" }
  /** They agreed. This is the event that spends money. */
  | { readonly type: "confirm" }
  | { readonly type: "accepted"; readonly executionId: string }
  | { readonly type: "refused"; readonly refusal: PanelRefusal }
  /**
   * The request never produced an answer at all - it threw.
   *
   * Kept apart from `refused`, which is the server saying no. This is not
   * knowing, and the two cannot share a sentence: a refusal means nothing
   * started, and this means nobody can say.
   */
  | { readonly type: "failed" };

export const INITIAL_PANEL_STATE: PanelState = { step: "idle" };

/**
 * The next state, or the current one when the event does not apply.
 *
 * ```
 *  idle ──submit──▶ confirming ──confirm──▶ pending ──┬─accepted─▶ accepted
 *    ▲                   │                            ├─refused──▶ refused | blocked
 *    │                   │                            └─failed───▶ refused (uncertain)
 *    └─────cancel────────┘                                             │
 *    ◀──────────────── submit (run again) ─────────────────────────────┘
 * ```
 *
 * **The transitions that deliberately do nothing are the point of this
 * function**, not an afterthought in a `default` branch:
 *
 * - **`confirm` while `pending`** is the second click on "Yes, run it", and
 *   the one that would buy a second provider call. The component also holds
 *   a synchronous ref, because `disabled` only takes effect on the next
 *   render; this is the half of that guard a test can reach.
 * - **`submit` while `pending` or `confirming`** is the same double-click
 *   one button earlier.
 * - **`cancel` while `pending`** does nothing on purpose. The request is on
 *   the wire and this panel cannot recall it - the execution's own cancel
 *   route could, and Stage B does not build it, so offering a "cancel" that
 *   only cleared the screen would be a lie about what it stopped.
 * - **A result arriving outside `pending`** is a reply to a request this
 *   panel is no longer waiting for, and applying it would overwrite whatever
 *   the person is looking at now. `failed` is a result like the other two and
 *   is ignored in the same places - `finishTest`'s `catch` runs whether or
 *   not the store is still `pending` when the request that is ending
 *   finally does, so it can arrive late.
 *
 * **`blocked` is terminal.** It is reached when the API refuses with a code
 * `testRefusal` recognises - archived, `observed`, `dispatchable` - which
 * means the gate should have caught it and the agent has changed underneath
 * this page. Offering the form again would invite the same refusal and the
 * same wait; the notice is the honest end of the sequence, and a reload is
 * how the page catches up.
 */
export function panelStep(state: PanelState, event: PanelEvent): PanelState {
  switch (state.step) {
    case "idle":
    case "accepted":
    case "refused":
      // "Run it again" starts the sequence over, confirmation included: a
      // second test costs what the first one did.
      return event.type === "submit" ? { step: "confirming" } : state;

    case "confirming":
      if (event.type === "cancel") return INITIAL_PANEL_STATE;
      if (event.type === "confirm") return { step: "pending" };
      return state;

    case "pending":
      if (event.type === "accepted") return { step: "accepted", executionId: event.executionId };
      if (event.type === "refused") return refusedState(event.refusal);
      if (event.type === "failed") {
        return {
          step: "refused",
          message: uncertainFailureMessage(),
          field: null,
          requestId: null,
        };
      }
      return state;

    case "blocked":
      return state;
  }
}

/**
 * What to say when the request itself threw.
 *
 * **It does not say the test did not start, because nobody here knows.** The
 * throw can land on either side of the endpoint: before the request left the
 * browser, or after the API had already created the execution and published
 * the harness. A sentence claiming nothing happened would be a guess in the
 * expensive direction - somebody reading it retries, and a provider is paid
 * twice for one question.
 *
 * So it sends them to the one place that holds the answer. The executions
 * card is on this same page, directly below, and a test that started is in
 * it once the page is reloaded.
 *
 * And nothing retries on its own. A retry after an uncertain failure is a
 * decision about spending money, which belongs to the person, not to a
 * `catch`.
 */
export function uncertainFailureMessage(): string {
  return (
    "The request did not come back, so this cannot tell you whether the test started. " +
    "It may already be running. Reload this page and look at the executions below " +
    "before trying again - a second attempt would be a second run and a second charge."
  );
}

function refusedState(refusal: PanelRefusal): PanelState {
  const known = testRefusal(refusal.codes);
  if (known !== null) {
    return { step: "blocked", notice: { headline: known.headline, detail: known.detail } };
  }
  return {
    step: "refused",
    message: refusal.message,
    field: refusal.field ?? null,
    requestId: refusal.requestId ?? null,
  };
}

/**
 * The synchronous in-flight flag, as this module needs to see it.
 *
 * Structurally a `useRef`, named rather than imported so nothing in `lib/`
 * depends on React. It is deliberately mutable: that is the whole point of
 * it - a `useState` cannot be read back inside the handler that set it.
 */
export interface PanelGuard {
  current: boolean;
}

/**
 * The panel's state, held in one place that is always current.
 *
 * **Why a store and not a `PanelState` argument.** The previous version of
 * `beginTest` took the state as a value, and the component passed the one
 * captured by the render that built the handler. Its dispatcher, meanwhile,
 * applied events through a functional `setState`, which React runs against
 * whatever is *committed*. Two sources of truth, and a measurable hole
 * between them: a confirmation from a render that a cancel had already
 * overtaken judged itself allowed, took the request guard and sent a
 * request, while the `confirm` it dispatched was dropped on the committed
 * `idle` - leaving a charged provider call and a panel showing nothing. It
 * was reproduced in the browser before this was written.
 *
 * Passing the right value was never going to be enough, because nothing in
 * this project can test what a component passes: vitest here is node-only.
 * So the state lives here instead. `send` computes from `current`, assigns
 * the result back before it returns, and only then publishes it, so the
 * value `beginTest` reads is the value the next `send` will build on. There
 * is no parameter left for a stale snapshot to arrive through.
 *
 * `publish` is React's `setState`. It is called with the already-computed
 * state rather than an updater, which is also what keeps the `pending`
 * render urgent - see `beginTest`.
 */
export interface PanelStore {
  /** The authoritative state, as of now. */
  current(): PanelState;
  /** Apply an event and return the state it produced. */
  send(event: PanelEvent): PanelState;
}

export function createPanelStore(publish: (next: PanelState) => void): PanelStore {
  let current: PanelState = INITIAL_PANEL_STATE;
  return {
    current: () => current,
    send(event) {
      // Assigned before publishing, so a handler that runs before React has
      // re-rendered still reads the state its predecessor produced.
      current = panelStep(current, event);
      publish(current);
      return current;
    },
  };
}

/**
 * Take the guard and go to `pending`, or answer that there is nothing to do.
 *
 * **Synchronous, and called outside any transition.** Both halves of that
 * matter. Synchronous, because `disabled` only applies on the next render
 * and the second click of a double-click lands before it, so the guard has
 * to be taken in the handler itself. Outside a transition, because an update
 * dispatched inside one is not urgent: React holds it until the async action
 * settles, and the "Starting…" render it describes would never be painted.
 * That is not a theory - it was measured on the running app, with the
 * in-flight controls still reading "Yes, run the test" throughout.
 *
 * Returns whether the caller should go on to `finishTest` - read off the
 * state the store produced, not off the event this asked it for.
 */
export function beginTest({
  guard,
  store,
}: {
  readonly guard: PanelGuard;
  readonly store: PanelStore;
}): boolean {
  // First, and never touched afterwards unless this call is the one that
  // took it: a held guard belongs to a request already on the wire, and
  // releasing somebody else's is how two requests end up in flight.
  if (guard.current) return false;

  // Read from the store, never from a caller's snapshot: this is the check a
  // confirmation overtaken by a cancel has to fail. It is also the machine's
  // own answer to "may this start now?", so a confirm that was never asked
  // for does not take a guard it would then have to release, and no no-op is
  // published to React.
  const now = store.current();
  if (panelStep(now, { type: "confirm" }) === now) return false;

  guard.current = true;
  // **Success is the store's answer, not the event's.** Asking and being
  // obeyed are two different things, and the regression this store was built
  // for was exactly the gap between them. Unreachable while the store agrees
  // with `panelStep` - which is why a test supplies one that does not.
  if (store.send({ type: "confirm" }).step !== "pending") {
    // Only ever the guard this call took, two lines up.
    guard.current = false;
    return false;
  }
  return true;
}

/**
 * Perform the request, fold the answer back, and release the guard.
 *
 * **This is in `lib/` because of what it has to get right.** Two of its
 * three steps are invisible in a passing happy path: the `catch` that keeps
 * a thrown request from reading as "it did not start", and the `finally`
 * that releases the guard whichever way the request ended. Written inside
 * the `.tsx` they would be untestable in this project - vitest here is
 * node-only - and the previous round of this work proved what that costs: a
 * component with no `finally` at all, under a green suite.
 *
 * The caller supplies the parts that are React's or Next's:
 *
 * - `run` performs the request and classifies the answer. It returns an
 *   event rather than a result, so this never has to know what an
 *   `ActionResult` is.
 * - `rethrow` is given `next/navigation`'s `unstable_rethrow` by the
 *   component, and it is not optional in spirit. **A server action that
 *   redirects rejects its promise** - `reject(getRedirectError(...))`, in
 *   Next's own `server-action-reducer` - which is how an expired session
 *   reaches the sign-in page from an action. Swallowing it would strand
 *   somebody on "it may already be running" for a request that never got
 *   past `requireSession`. Rethrowing leaves this panel behaving exactly as
 *   the lifecycle and edit forms beside it, which do not catch at all. The
 *   caller runs this inside a transition so React has somewhere to take it.
 * - `onAccepted` is the router refresh. It runs only when the store actually
 *   reached `accepted`, which is not the same as the request having answered
 *   202: a reply the panel has stopped waiting for is dropped by the machine,
 *   and this follows the machine.
 *
 * Note the order: the rethrow comes first, and the `finally` still runs on
 * the way out, so even a redirect releases the guard.
 */
export async function finishTest({
  guard,
  store,
  run,
  rethrow,
  onAccepted,
}: {
  readonly guard: PanelGuard;
  readonly store: PanelStore;
  readonly run: () => Promise<PanelEvent>;
  readonly rethrow?: ((error: unknown) => void) | undefined;
  readonly onAccepted?: (() => void) | undefined;
}): Promise<void> {
  try {
    const event = await run();
    // **The store's answer decides, not the event's type.** An `accepted`
    // arriving when the store is no longer `pending` is a reply to a request
    // this panel has stopped waiting for; the machine drops it, and a
    // refresh fired on the strength of the event alone would reload the page
    // for a run nothing here is showing.
    const next = store.send(event);
    if (next.step === "accepted") onAccepted?.();
  } catch (error) {
    rethrow?.(error);
    store.send({ type: "failed" });
  } finally {
    guard.current = false;
  }
}

/**
 * Whether a request is in flight, for a control that should be disabled.
 *
 * A named question rather than `state.step === "pending"` spelled out at
 * four call sites, so the component and its tests ask it the same way.
 */
export function panelBusy(state: PanelState): boolean {
  return state.step === "pending";
}

/**
 * Whether the sample box should still be offered.
 *
 * False only once the panel is `blocked`: a refusal about the agent's mode
 * or its archive is not something a different sample would get past.
 */
export function panelAcceptsInput(state: PanelState): boolean {
  return state.step !== "blocked";
}
