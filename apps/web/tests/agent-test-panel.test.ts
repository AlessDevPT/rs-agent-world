/**
 * The agent test panel's view selector and its submission sequence.
 *
 * Pure, like the module it covers. Nothing here renders a component - there
 * is no DOM in this environment and no DOM-testing dependency in the
 * project - which is exactly why the panel's decisions were put in
 * `lib/agents/test-panel.ts` rather than in the `.tsx` beside it.
 *
 * **The transition table is exhaustive on purpose.** The event that must do
 * nothing is worth more here than the event that must work: `confirm` while
 * `pending` is the second click that would buy a second provider call, and
 * a reducer that quietly fell through to a default would pass a test written
 * only for the happy path. So every event is asserted against every state.
 */

import { describe, expect, it } from "vitest";

import {
  INITIAL_PANEL_STATE,
  missingProfileWarning,
  panelAcceptsInput,
  panelBusy,
  beginTest,
  createPanelStore,
  finishTest,
  panelStep,
  testPanelView,
  uncertainFailureMessage,
  type PanelAgent,
  type PanelEvent,
  type PanelState,
  type PanelStore,
} from "@/lib/agents/test-panel";
import { testFindingSentence, testWarning } from "@/lib/agents/test";
import type { AgentInteractionMode } from "@/lib/api/agents";

function agent(
  interactionMode: AgentInteractionMode,
  { archived = false, profile = true }: { archived?: boolean; profile?: boolean } = {},
): PanelAgent {
  return {
    interactionMode,
    archivedAt: archived ? "2026-11-18T09:00:00Z" : null,
    hasModelProfile: profile,
  };
}

const MANAGED = agent("managed");

// ------------------------------------------------------------ the selector ---

describe("what the panel shows", () => {
  it("offers the form to a member looking at a managed agent", () => {
    const view = testPanelView(MANAGED, "member");
    expect(view.kind).toBe("form");
    if (view.kind !== "form") return;
    expect(view.warning).toEqual(testWarning());
    expect(view.profileWarning).toBeNull();
  });

  it("offers it to admins and owners too", () => {
    for (const role of ["admin", "owner"] as const) {
      expect(testPanelView(MANAGED, role).kind).toBe("form");
    }
  });

  it("gives a viewer the role notice and no form", () => {
    const view = testPanelView(MANAGED, "viewer");
    expect(view.kind).toBe("notice");
    if (view.kind !== "notice") return;
    expect(view.notice.detail).toMatch(/member role/i);
    expect(view.notice.detail).toMatch(/viewer/);
    // No invented headline beside a sentence that already explains itself.
    expect(view.notice.headline).toBeNull();
  });

  it("tells a viewer about their role rather than about the agent's mode", () => {
    // The API authorizes before it reads the agent, so the role is the
    // refusal they would actually get; leading with the mode would promise
    // that changing the mode would help them.
    const view = testPanelView(agent("observed"), "viewer");
    expect(view.kind).toBe("notice");
    if (view.kind !== "notice") return;
    expect(view.notice.detail).toMatch(/member role/i);
    expect(view.notice.detail).not.toMatch(/observed/i);
  });

  it("refuses an observed agent with its own explanation", () => {
    const view = testPanelView(agent("observed"), "member");
    expect(view.kind).toBe("notice");
    if (view.kind !== "notice") return;
    expect(view.notice.headline).toBe(testFindingSentence("observed_agent")?.headline);
    expect(view.notice.detail).toBe(testFindingSentence("observed_agent")?.detail);
  });

  it("refuses a dispatchable agent with its own explanation", () => {
    const view = testPanelView(agent("dispatchable"), "member");
    expect(view.kind).toBe("notice");
    if (view.kind !== "notice") return;
    expect(view.notice.detail).toBe(testFindingSentence("dispatchable_agent")?.detail);
  });

  it("refuses an archived agent whatever its mode", () => {
    for (const mode of ["managed", "observed", "dispatchable"] as const) {
      const view = testPanelView(agent(mode, { archived: true }), "admin");
      expect(view.kind).toBe("notice");
      if (view.kind !== "notice") continue;
      expect(view.notice.detail).toBe(testFindingSentence("agent_archived")?.detail);
    }
  });

  it("puts the archive before the mode, as the compiler does", () => {
    // `_eligible` checks archived first and returns, so a gate that said
    // "observed" here would disagree with the 422 that follows it.
    const view = testPanelView(agent("observed", { archived: true }), "admin");
    if (view.kind !== "notice") throw new Error("expected a notice");
    expect(view.notice.detail).toBe(testFindingSentence("agent_archived")?.detail);
  });

  it("puts the role before everything, including the archive", () => {
    const view = testPanelView(agent("managed", { archived: true }), "viewer");
    if (view.kind !== "notice") throw new Error("expected a notice");
    expect(view.notice.detail).toMatch(/member role/i);
  });

  it("warns before spending when the agent has no model profile", () => {
    const view = testPanelView(agent("managed", { profile: false }), "member");
    expect(view.kind).toBe("form");
    if (view.kind !== "form") return;
    expect(view.profileWarning).toBe(missingProfileWarning());
  });

  it("says the test will probably fail, never that it will", () => {
    // The missing-credential path is covered by an API integration test; the
    // missing-profile path is read off the runner and is not, so the copy
    // stops short of promising the outcome.
    const warning = missingProfileWarning();
    expect(warning).toMatch(/probably/i);
    expect(warning).not.toMatch(/will fail|cannot succeed|is going to fail/i);
    // And no price, invented or otherwise.
    expect(warning).not.toMatch(/\$|cents?|per token/i);
  });
});

// -------------------------------------------------------------- the machine ---

const EVENTS: readonly PanelEvent[] = [
  { type: "submit" },
  { type: "cancel" },
  { type: "confirm" },
  { type: "accepted", executionId: "e-1" },
  { type: "refused", refusal: { message: "no" } },
  { type: "failed" },
];

const STATES: readonly PanelState[] = [
  { step: "idle" },
  { step: "confirming" },
  { step: "pending" },
  { step: "accepted", executionId: "e-0" },
  { step: "refused", message: "earlier", field: null, requestId: null },
  { step: "blocked", notice: { headline: "h", detail: "d" } },
];

describe("the submission sequence", () => {
  it("starts idle", () => {
    expect(INITIAL_PANEL_STATE).toEqual({ step: "idle" });
  });

  it("asks for confirmation before anything is sent", () => {
    expect(panelStep({ step: "idle" }, { type: "submit" })).toEqual({ step: "confirming" });
  });

  it("lets the confirmation be cancelled", () => {
    expect(panelStep({ step: "confirming" }, { type: "cancel" })).toEqual({ step: "idle" });
  });

  it("goes in flight only on confirm", () => {
    expect(panelStep({ step: "confirming" }, { type: "confirm" })).toEqual({ step: "pending" });
  });

  it("carries the execution id on acceptance", () => {
    expect(panelStep({ step: "pending" }, { type: "accepted", executionId: "e-9" })).toEqual({
      step: "accepted",
      executionId: "e-9",
    });
  });

  it("carries the message, field and request id on a refusal", () => {
    const next = panelStep(
      { step: "pending" },
      {
        type: "refused",
        refusal: { message: "A sample may be at most 20000 characters", field: "input", requestId: "req-7" },
      },
    );
    expect(next).toEqual({
      step: "refused",
      message: "A sample may be at most 20000 characters",
      field: "input",
      requestId: "req-7",
    });
  });

  it("normalises an absent field and request id to null rather than undefined", () => {
    const next = panelStep({ step: "pending" }, { type: "refused", refusal: { message: "no" } });
    expect(next).toEqual({ step: "refused", message: "no", field: null, requestId: null });
  });

  it("blocks instead of refusing when the code is one the gate knows", () => {
    for (const code of ["agent_archived", "observed_agent", "dispatchable_agent"] as const) {
      const next = panelStep(
        { step: "pending" },
        { type: "refused", refusal: { message: "raw", codes: [code] } },
      );
      expect(next.step).toBe("blocked");
      if (next.step !== "blocked") continue;
      expect(next.notice.detail).toBe(testFindingSentence(code)?.detail);
      // The API's own sentence is dropped in favour of the one the gate
      // would have shown, so both routes to this refusal read the same.
      expect(next.notice.detail).not.toBe("raw");
    }
  });

  it("refuses rather than blocks on a code it does not know", () => {
    const next = panelStep(
      { step: "pending" },
      { type: "refused", refusal: { message: "something new", codes: ["some_future_code"] } },
    );
    expect(next.step).toBe("refused");
  });

  it("turns a thrown request into a refusal that admits it does not know", () => {
    const next = panelStep({ step: "pending" }, { type: "failed" });
    expect(next).toEqual({
      step: "refused",
      message: uncertainFailureMessage(),
      field: null,
      requestId: null,
    });
  });

  it("never says a thrown request started nothing", () => {
    // The sentence this panel must not produce. A throw can land on either
    // side of the endpoint, and "nothing started" read by somebody who then
    // retries is a second execution and a second provider charge.
    const message = uncertainFailureMessage();
    expect(message).not.toMatch(
      /did not start|was not started|nothing (was )?started|no execution|failed to start/i,
    );
    // It says what is actually known, and where the answer is.
    expect(message).toMatch(/may already be running|cannot tell you whether/i);
    expect(message).toMatch(/executions/i);
  });

  it("lets a deliberate retry follow an uncertain failure, and only a deliberate one", () => {
    const failed = panelStep({ step: "pending" }, { type: "failed" });
    // Nothing retries on its own: the only way forward is the ordinary
    // submit, which goes through the confirmation again.
    expect(panelStep(failed, { type: "confirm" })).toBe(failed);
    expect(panelStep(failed, { type: "submit" })).toEqual({ step: "confirming" });
  });

  it("lets a finished run be started again, through confirmation", () => {
    expect(panelStep({ step: "accepted", executionId: "e-1" }, { type: "submit" })).toEqual({
      step: "confirming",
    });
    expect(
      panelStep({ step: "refused", message: "no", field: null, requestId: null }, { type: "submit" }),
    ).toEqual({ step: "confirming" });
  });
});

describe("the transitions that must do nothing", () => {
  it("ignores a second confirm while a request is in flight", () => {
    // The click that would buy a second provider call. There is no
    // idempotency key on this endpoint, so two requests are two executions.
    const pending: PanelState = { step: "pending" };
    expect(panelStep(pending, { type: "confirm" })).toBe(pending);
  });

  it("ignores a second submit while confirming or pending", () => {
    const confirming: PanelState = { step: "confirming" };
    const pending: PanelState = { step: "pending" };
    expect(panelStep(confirming, { type: "submit" })).toBe(confirming);
    expect(panelStep(pending, { type: "submit" })).toBe(pending);
  });

  it("ignores a cancel once the request is on the wire", () => {
    // Nothing here can recall it, and clearing the screen would claim to
    // have stopped something it did not.
    const pending: PanelState = { step: "pending" };
    expect(panelStep(pending, { type: "cancel" })).toBe(pending);
  });

  it("ignores a confirm that was never asked for", () => {
    const idle: PanelState = { step: "idle" };
    expect(panelStep(idle, { type: "confirm" })).toBe(idle);
    expect(panelStep(idle, { type: "cancel" })).toBe(idle);
  });

  it("ignores a result that arrives outside a request", () => {
    // A reply to something this panel is no longer waiting for would
    // overwrite whatever the person is looking at now.
    for (const state of STATES.filter((s) => s.step !== "pending")) {
      expect(panelStep(state, { type: "accepted", executionId: "e-late" })).toBe(state);
      expect(panelStep(state, { type: "refused", refusal: { message: "late" } })).toBe(state);
      // `failed` comes from a `finally`, which runs whether or not this
      // panel is still waiting for the request that is ending.
      expect(panelStep(state, { type: "failed" })).toBe(state);
    }
  });

  it("stays blocked whatever happens next", () => {
    const blocked: PanelState = { step: "blocked", notice: { headline: "h", detail: "d" } };
    for (const event of EVENTS) {
      expect(panelStep(blocked, event)).toBe(blocked);
    }
  });

  it("is total: every event against every state returns a valid state", () => {
    const steps = new Set(["idle", "confirming", "pending", "accepted", "refused", "blocked"]);
    for (const state of STATES) {
      for (const event of EVENTS) {
        const next = panelStep(state, event);
        expect(next).toBeDefined();
        expect(steps.has(next.step)).toBe(true);
      }
    }
  });
});

describe("what the panel asks about its own state", () => {
  it("is busy only while the request is in flight", () => {
    expect(panelBusy({ step: "pending" })).toBe(true);
    for (const state of STATES.filter((s) => s.step !== "pending")) {
      expect(panelBusy(state)).toBe(false);
    }
  });

  it("stops accepting input only once blocked", () => {
    for (const state of STATES.filter((s) => s.step !== "blocked")) {
      expect(panelAcceptsInput(state)).toBe(true);
    }
    expect(panelAcceptsInput({ step: "blocked", notice: { headline: null, detail: "d" } })).toBe(
      false,
    );
  });
});

/**
 * The submission itself: the store, the guard, the `catch` and the `finally`.
 *
 * These live in `lib/` rather than in the component for exactly the reason
 * this block demonstrates - every one of them is invisible when things go
 * well, and an earlier round of this work shipped a component with no
 * `finally` at all under a green suite. Removing any one of them now fails a
 * test, which was verified by removing each and watching it go red:
 *
 * | broken in the real code                 | what fails                      |
 * | --------------------------------------- | ------------------------------- |
 * | `current()` made to lag one event, which | both overtaken-confirm tests,   |
 * | is the original defect exactly           | on `runs` and on the request    |
 * | `beginTest`'s `if (guard.current)`       | the held-guard test             |
 * | the cleanup on the thrown path           | 4 tests, here and in the action |
 * | the `finally`, cleanup kept elsewhere    | the redirect case leaks a guard |
 * | `rethrow?.(error)`                       | "resolved instead of rejecting" |
 *
 * The first of those is the one worth reading twice. A store whose `current()`
 * answers with the state as of the previous event *is* a render-captured
 * snapshot, so that mutation puts the old bug back: the decision sees
 * `confirming`, the dispatch lands on `idle`, a request goes out and its
 * outcome is dropped. Both tests then fail on the thing that costs money -
 * one request made where none should be.
 *
 * The split into two functions is not decoration either. `beginTest` is
 * synchronous and runs outside React's transition so its `pending` update is
 * urgent and actually paints; `finishTest` is awaited inside one so a
 * redirect has somewhere to go. Folding them together was tried on the
 * running app, and the in-flight controls never changed at all.
 */
describe("running one submission", () => {
  function harness(run: () => Promise<PanelEvent>, step: "confirming" | "idle" = "confirming") {
    const published: PanelState[] = [];
    const store = createPanelStore((next) => {
      published.push(next);
    });
    const guard = { current: false };
    const refreshes: true[] = [];
    if (step === "confirming") store.send({ type: "submit" });
    return {
      guard,
      store,
      published,
      refreshes,
      get state(): PanelState {
        return store.current();
      },
      async call(rethrow?: (error: unknown) => void): Promise<void> {
        if (!beginTest({ guard, store })) return;
        await finishTest({
          guard,
          store,
          run,
          ...(rethrow === undefined ? {} : { rethrow }),
          onAccepted: () => {
            refreshes.push(true);
          },
        });
      },
    };
  }

  it("goes pending, accepts, and refreshes once", async () => {
    const h = harness(async () => ({ type: "accepted", executionId: "e-1" }));
    await h.call();
    expect(h.state).toEqual({ step: "accepted", executionId: "e-1" });
    expect(h.refreshes).toHaveLength(1);
    expect(h.guard.current).toBe(false);
  });

  it("does not refresh after a refusal", async () => {
    const h = harness(async () => ({ type: "refused", refusal: { message: "no" } }));
    await h.call();
    expect(h.state.step).toBe("refused");
    expect(h.refreshes).toHaveLength(0);
    expect(h.guard.current).toBe(false);
  });

  it("releases the guard when the request throws, and says it does not know", async () => {
    // Delete the `finally` and this is one of the assertions that goes red.
    const h = harness(() => Promise.reject(new Error("socket hang up")));
    await h.call();
    expect(h.guard.current).toBe(false);
    expect(h.state.step).toBe("refused");
    if (h.state.step !== "refused") return;
    expect(h.state.message).toBe(uncertainFailureMessage());
    expect(h.refreshes).toHaveLength(0);
  });

  it("hands a redirect back out instead of calling it an uncertain failure", async () => {
    // Next rejects a server action's promise when the action redirects -
    // `reject(getRedirectError(...))` in its own `server-action-reducer` -
    // which is how an expired session reaches sign-in from here. Swallowing
    // it would strand somebody on "it may already be running" for a request
    // that never got past `requireSession`.
    //
    // Shaped the way Next shapes one: the marker is on `digest`, which is
    // what `isRedirectError` reads and what `unstable_rethrow` - the real
    // function the component passes - tests before rethrowing.
    const redirect = Object.assign(new Error("NEXT_REDIRECT"), {
      digest: "NEXT_REDIRECT;replace;/login;307;",
    });
    const h = harness(() => Promise.reject(redirect));

    await expect(
      h.call((error) => {
        if (
          typeof error === "object" &&
          error !== null &&
          String((error as { digest?: unknown }).digest).startsWith("NEXT_REDIRECT")
        ) {
          throw error;
        }
      }),
    ).rejects.toBe(redirect);

    // Still released, because the rethrow leaves through the `finally`.
    expect(h.guard.current).toBe(false);
    // And no sentence was invented for a page that is navigating away.
    expect(h.state).toEqual({ step: "pending" });
  });

  it("starts one request when it is confirmed twice before the first answers", async () => {
    let runs = 0;
    const h = harness(async () => {
      runs += 1;
      await Promise.resolve();
      return { type: "accepted", executionId: "e-1" };
    });

    await Promise.all([h.call(), h.call()]);

    expect(runs).toBe(1);
    expect(h.guard.current).toBe(false);
  });

  it("will not start while the guard is held, whatever the state allows", () => {
    // The half of the double-click defence the test above can no longer
    // isolate: now that the store is authoritative, the machine would also
    // refuse a second confirm, so only a held guard over a state that still
    // permits one shows that this check is doing anything.
    const store = createPanelStore(() => undefined);
    store.send({ type: "submit" });
    const guard = { current: true };

    expect(beginTest({ guard, store })).toBe(false);
    expect(store.current()).toEqual({ step: "confirming" });
    // And it is left held. The request that took it is still on the wire,
    // and releasing another call's guard is how two requests overlap.
    expect(guard.current).toBe(true);
  });

  it("does nothing, and takes no guard, when there was nothing to confirm", async () => {
    let runs = 0;
    const h = harness(async () => {
      runs += 1;
      return { type: "failed" };
    }, "idle");

    await h.call();

    expect(runs).toBe(0);
    expect(h.guard.current).toBe(false);
    expect(h.state).toEqual({ step: "idle" });
  });

  it("reaches pending before the request is awaited at all", () => {
    // The render that shows "Starting..." and disables the controls. It has
    // to happen in the click handler, not when the action settles.
    const store = createPanelStore(() => undefined);
    store.send({ type: "submit" });
    const guard = { current: false };

    expect(beginTest({ guard, store })).toBe(true);
    expect(store.current()).toEqual({ step: "pending" });
    expect(guard.current).toBe(true);
  });

  it("ignores a confirmation that a cancel has already overtaken", async () => {
    // **The regression this store exists for.** The old `beginTest` took the
    // state as a value, and the component handed it the one captured by the
    // render that built the handler. Cancel, then a confirmation from the
    // render before it: the snapshot said `confirming`, so it judged itself
    // allowed, took the guard and sent a request - while the `confirm` it
    // dispatched was dropped on the committed `idle`, and so was every
    // event after it. A charged provider call, and a panel showing nothing.
    // Reproduced in the browser, which is what prompted this test.
    let runs = 0;
    const h = harness(async () => {
      runs += 1;
      return { type: "accepted", executionId: "e-1" };
    });

    // The render that built the stale handler put this on the screen.
    expect(h.published.map((entry) => entry.step)).toEqual(["confirming"]);
    h.store.send({ type: "cancel" });

    await h.call();

    // **No request, and nothing spent.** This is the assertion that fails
    // against the old behaviour; every other one here is corroboration.
    expect(runs).toBe(0);
    // The guard was never taken, so the panel is not stuck either.
    expect(h.guard.current).toBe(false);
    // The cancel stands, and no `pending` was ever published - nothing
    // showed "Starting..." over a request that does not exist, and no
    // outcome was dropped on a state that had moved on. Asserted on what
    // React was handed rather than on the store, so that a store which
    // lied about its own state could not make this pass.
    expect(h.published.map((entry) => entry.step)).toEqual(["confirming", "idle"]);
    expect(h.state).toEqual({ step: "idle" });
  });
  it("does not refresh when the store ignored the acceptance", async () => {
    // A 202 for a request this panel has stopped waiting for. The machine
    // drops a result that arrives outside `pending`, and the refresh has to
    // drop with it - firing it here would reload the page for a run nothing
    // on screen is showing. The event says `accepted`; the store does not.
    const h = harness(async () => ({ type: "accepted", executionId: "e-1" }), "idle");

    await finishTest({
      guard: h.guard,
      store: h.store,
      run: async () => ({ type: "accepted", executionId: "e-1" }),
      onAccepted: () => {
        h.refreshes.push(true);
      },
    });

    expect(h.state).toEqual({ step: "idle" });
    expect(h.refreshes).toHaveLength(0);
    expect(h.guard.current).toBe(false);
  });

  it("releases the guard it took when the store ignores the confirmation", () => {
    // A store that disagrees with itself: `current()` says a confirm applies,
    // `send` refuses it. No real store does this - `createPanelStore` is
    // `panelStep` and nothing else - which is the point. `beginTest` must
    // believe the answer rather than the question, or it would report success
    // and strand the guard it had already taken.
    const liar: PanelStore = {
      current: () => ({ step: "confirming" }),
      send: () => ({ step: "idle" }),
    };
    const guard = { current: false };

    expect(beginTest({ guard, store: liar })).toBe(false);
    expect(guard.current).toBe(false);
  });

  it("lets the next valid attempt through after one was ignored", async () => {
    // The release above has to be real: an attempt refused for any reason
    // must not cost the panel its ability to start the next one.
    let runs = 0;
    const h = harness(async () => {
      runs += 1;
      return { type: "accepted", executionId: "e-2" };
    });

    h.store.send({ type: "cancel" });
    await h.call();
    expect(runs).toBe(0);
    expect(h.guard.current).toBe(false);

    h.store.send({ type: "submit" });
    await h.call();

    expect(runs).toBe(1);
    expect(h.state).toEqual({ step: "accepted", executionId: "e-2" });
    expect(h.refreshes).toHaveLength(1);
    expect(h.guard.current).toBe(false);
  });
});
