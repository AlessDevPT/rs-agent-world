/**
 * `testAgentAction`, and the one thing it does differently from every other
 * agent action beside it.
 *
 * Mocked down to the two things an action cannot do in a test - resolve a
 * session and revalidate a route - with the real API client underneath and
 * `fetch` stubbed. **No provider is reached and no execution is started:**
 * every response here is a literal written in this file.
 *
 * **Why the revalidation assertions are so specific.** The other agent
 * actions discard two routes, the agent's page and the project's list,
 * because an edit or an archive changes what the list says. A test changes
 * neither: it writes no status, no liveness and no heartbeat history, and
 * `GET .../status-history` stays empty after one. So this action discards the
 * detail route alone, and the test that the list is *not* revalidated is the
 * point rather than a detail - it is the assertion that would catch somebody
 * copying `archiveAgentAction` to write the next one.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/session", () => ({
  requireSession: vi.fn(async () => ({ token: "token-1", me: { workspaces: [] } })),
}));

import { revalidatePath } from "next/cache";

import { testAgentAction } from "@/app/actions";
import { SAMPLE_MAX, testFindingSentence, testRefusal } from "@/lib/agents/test";
import {
  beginTest,
  createPanelStore,
  finishTest,
  uncertainFailureMessage,
  type PanelEvent,
  type PanelState,
} from "@/lib/agents/test-panel";
import { requireSession } from "@/lib/session";

const AGENTS_PATH = "/w/w-1/p/p-1/agents";
const DETAIL_PATH = `${AGENTS_PATH}/a-1`;
const SAMPLE = "Summarise the fintech funding news.";

interface Call {
  readonly url: string;
  readonly init: { method?: string; body?: unknown; headers?: Record<string, string> };
}

function stubFetch(status: number, body: unknown): { calls: Call[] } {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: Call["init"]) => {
      calls.push({ url, init });
      return { ok: status < 400, status, statusText: "", json: async () => body };
    }),
  );
  return { calls };
}

function path(url: string): string {
  return url.replace(/^https?:\/\/[^/]+/, "");
}

/** The 202 body: an execution that exists and has answered nothing. */
const ACCEPTED = {
  id: "e-1",
  workflow_id: "w-9",
  workflow_version_id: "v-9",
  status: "running",
  initiator_kind: "user",
  initiator_label: "someone@example.com",
  input: { input: SAMPLE },
  output: null,
  error: null,
  created_at: "2026-11-19T10:00:00Z",
  started_at: "2026-11-19T10:00:00Z",
  finished_at: null,
};

function problem(status: number, detail: string, extra: object = {}): object {
  return {
    type: "https://agentworld.dev/problems/x",
    title: "Refused",
    status,
    detail,
    instance: "req-42",
    ...extra,
  };
}

beforeEach(() => {
  vi.mocked(revalidatePath).mockClear();
  vi.unstubAllGlobals();
});

describe("starting a test", () => {
  it("posts the sample and hands back the accepted execution", async () => {
    const { calls } = stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1/test");
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ input: SAMPLE });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.execution.id).toBe("e-1");
  });

  it("returns an acceptance rather than a result", async () => {
    // The whole hazard of this endpoint: `ok: true` means the execution
    // exists, not that the agent answered. A caller reading this result must
    // be able to see that nothing has come back yet.
    stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.execution.output).toBeNull();
    expect(result.execution.finished_at).toBeNull();
    expect(["pending", "running"]).toContain(result.execution.status);
  });

  it("revalidates the agent's own page and nothing else", async () => {
    stubFetch(202, ACCEPTED);

    await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(vi.mocked(revalidatePath).mock.calls).toEqual([[DETAIL_PATH]]);
  });

  it("does not revalidate the project's agent list", async () => {
    // A test writes no status, liveness or heartbeat history, and the list
    // renders nothing else that a test could move.
    stubFetch(202, ACCEPTED);

    await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalledWith(AGENTS_PATH);
  });

  it("sends the sample exactly as typed", async () => {
    const typed = "  Summarise this:\n\n  - one\n";
    const { calls } = stubFetch(202, ACCEPTED);

    await testAgentAction("w-1", "p-1", "a-1", typed);

    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ input: typed });
  });
});

describe("samples it refuses without asking", () => {
  it("refuses an empty sample before any request", async () => {
    const { calls } = stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", "");

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.field).toBe("input");
    expect(calls).toHaveLength(0);
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
  });

  it("refuses a whitespace-only sample, which the API would accept and bill", async () => {
    const { calls } = stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", "   \n  ");

    expect(result.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it("refuses an over-long sample before any request", async () => {
    const { calls } = stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", "x".repeat(SAMPLE_MAX + 1));

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.field).toBe("input");
    expect(result.status).toBeUndefined();
    expect(calls).toHaveLength(0);
  });

  it("sends a sample of exactly the maximum length", async () => {
    const { calls } = stubFetch(202, ACCEPTED);

    await testAgentAction("w-1", "p-1", "a-1", "x".repeat(SAMPLE_MAX));

    expect(calls).toHaveLength(1);
  });
});

describe("refusals from the API", () => {
  it("returns a 403 as a failure, with its status and request id", async () => {
    stubFetch(403, problem(403, "Testing an agent needs the member role"));

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result).toMatchObject({
      ok: false,
      message: "Testing an agent needs the member role",
      status: 403,
      requestId: "req-42",
    });
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
  });

  it("returns a 404 unchanged rather than guessing why", async () => {
    stubFetch(404, problem(404, "Agent not found"));

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result).toMatchObject({ ok: false, status: 404, message: "Agent not found" });
  });

  it("returns a 409 for a slug an author already took", async () => {
    stubFetch(
      409,
      problem(409, "This project already has a workflow with the slug 'test-writer'"),
    );

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result).toMatchObject({ ok: false, status: 409 });
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
  });

  it("returns a schema 422 with the sentence, and no field, because that handler sends none", async () => {
    // FastAPI's request-validation handler passes no locator at all - the
    // field is only inside `errors[].pointer` - so a top-level `field` here
    // would be a fixture describing a response the API does not send.
    stubFetch(
      422,
      problem(422, "1 field(s) failed validation", {
        errors: [{ pointer: "/input", code: "string_too_long", detail: "is too long" }],
      }),
    );

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result).toMatchObject({ ok: false, status: 422 });
    if (result.ok) return;
    expect(result.field).toBeUndefined();
    expect(result.message).toContain("is too long");
    // Not rewritten: `string_too_long` is not a refusal this release
    // explains, so the API's own sentence stands.
    expect(result.codes).toEqual(["string_too_long"]);
  });

  it("never revalidates after a failure", async () => {
    for (const status of [403, 404, 409, 422]) {
      vi.mocked(revalidatePath).mockClear();
      stubFetch(status, problem(status, "no"));
      await testAgentAction("w-1", "p-1", "a-1", SAMPLE);
      expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
    }
  });
});

describe("the structured refusal codes", () => {
  function notTestable(code: string): object {
    return problem(422, "This agent cannot be tested", {
      title: "Agent not testable",
      errors: [
        {
          pointer: "/agents/9a1f0c6e-0000-4000-8000-000000000001",
          code,
          detail: "'watcher' only reports its own activity",
        },
      ],
    });
  }

  it("carries the finding code so a screen can tell the three refusals apart", async () => {
    for (const code of ["agent_archived", "observed_agent", "dispatchable_agent"]) {
      stubFetch(422, notTestable(code));

      const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

      expect(result.ok).toBe(false);
      if (result.ok) continue;
      expect(result.codes).toEqual([code]);
      // And the code is enough to produce a sentence with a remedy in it,
      // which is the whole reason it is carried.
      expect(testRefusal(result.codes)).not.toBeNull();
      expect(result.message).toBe(testFindingSentence(code)?.detail);
    }
  });

  it("keeps the API's pointer and uuid out of the message a person reads", async () => {
    // `problemMessage` appends `errors[]` entries as "<pointer> <detail>",
    // and this endpoint's pointer is `/agents/<uuid>`. Every component
    // renders `message` directly, so the default would put a raw id on the
    // screen. This is the assertion that would catch it coming back.
    stubFetch(422, notTestable("observed_agent"));

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).not.toContain("/agents/");
    expect(result.message).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/i);
    expect(result.message).toBe(testRefusal(result.codes)?.detail);
  });

  it("shows the same words the gate would have shown", async () => {
    // A refusal caught by `agentTestability` before the request and the same
    // refusal returned by the API must read identically; two wordings for
    // one fact is how a screen starts contradicting itself.
    stubFetch(422, notTestable("agent_archived"));

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toBe(testFindingSentence("agent_archived")?.detail);
    expect(result.status).toBe(422);
    expect(result.requestId).toBe("req-42");
  });

  it("leaves a refusal it cannot explain in the API's own words", async () => {
    stubFetch(
      422,
      problem(422, "This agent cannot be tested", {
        errors: [{ pointer: "/agents/x", code: "some_future_code", detail: "a new reason" }],
      }),
    );

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toContain("a new reason");
    expect(result.codes).toEqual(["some_future_code"]);
  });

  it("leaves codes absent when the problem carried no findings", async () => {
    stubFetch(403, problem(403, "nope"));

    const result = await testAgentAction("w-1", "p-1", "a-1", SAMPLE);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.codes).toBeUndefined();
  });

  it("leaves codes absent on a refusal this layer made without asking", async () => {
    stubFetch(202, ACCEPTED);

    const result = await testAgentAction("w-1", "p-1", "a-1", "");

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.codes).toBeUndefined();
    expect(result.status).toBeUndefined();
  });
});

/**
 * The panel's sequence driving the real action, over a stubbed `fetch`.
 *
 * The closest this environment can get to an integration test: the store and
 * both halves of the submission from `lib/agents/test-panel.ts` are real, the
 * server action from `app/actions.ts` is real, and only the network is a
 * literal. Nothing renders - there is no DOM here - so what this proves is
 * the wiring the component will carry, not the markup it will carry it in.
 *
 * `drive()` is that wiring, and nothing in it is a copy of the production
 * logic: the guard, the state, the `catch` and the `finally` all belong to
 * `createPanelStore`, `beginTest` and `finishTest`. It no longer needs to
 * model React's stale render closure either, which earlier versions of this
 * file did at some length - the store is read at the moment it is used, so
 * there is no render-captured snapshot left for a handler to disagree with.
 * `tests/agent-test-panel.test.ts` covers what that now makes impossible.
 */
function drive(sample: string) {
  const published: PanelState[] = [];
  const store = createPanelStore((next) => {
    published.push(next);
  });
  const guard = { current: false };

  async function confirm(): Promise<void> {
    if (!beginTest({ guard, store })) return;
    await finishTest({
      guard,
      store,
      run: async () => {
        const result = await testAgentAction("w-1", "p-1", "a-1", sample);
        return result.ok
          ? { type: "accepted", executionId: result.execution.id }
          : { type: "refused", refusal: result };
      },
    });
  }

  return {
    send(event: PanelEvent) {
      store.send(event);
    },
    confirm,
    published,
    get state(): PanelState {
      return store.current();
    },
    get guardHeld(): boolean {
      return guard.current;
    },
  };
}

describe("the panel sequence against the real action", () => {
  it("sends one request and lands on the accepted execution", async () => {
    const { calls } = stubFetch(202, ACCEPTED);
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(calls).toHaveLength(1);
    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1/test");
    expect(panel.state).toEqual({ step: "accepted", executionId: "e-1" });
  });

  it("makes exactly one request when the confirm button is double-clicked", async () => {
    // Both calls start before either resolves, which is what a real
    // double-click does and what `disabled={pending}` cannot catch: the
    // attribute only applies after React re-renders. There is no
    // idempotency key on this endpoint, so a second request would be a
    // second execution and a second provider charge.
    const { calls } = stubFetch(202, ACCEPTED);
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await Promise.all([panel.confirm(), panel.confirm()]);

    expect(calls).toHaveLength(1);
    expect(panel.state).toEqual({ step: "accepted", executionId: "e-1" });
  });

  it("revalidates the detail path exactly once and never the agent list", async () => {
    stubFetch(202, ACCEPTED);
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await Promise.all([panel.confirm(), panel.confirm()]);

    expect(vi.mocked(revalidatePath).mock.calls).toEqual([[DETAIL_PATH]]);
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalledWith(AGENTS_PATH);
  });

  it("sends nothing at all when the sample is one the rules refuse", async () => {
    const { calls } = stubFetch(202, ACCEPTED);
    const panel = drive("   ");

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(calls).toHaveLength(0);
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
    expect(panel.state).toMatchObject({ step: "refused", field: "input" });
  });

  it("ends blocked, with the gate's words, when the agent changed underneath the page", async () => {
    stubFetch(
      422,
      problem(422, "This agent cannot be tested", {
        errors: [
          {
            pointer: "/agents/9a1f0c6e-0000-4000-8000-000000000001",
            code: "agent_archived",
            detail: "'writer' is archived",
          },
        ],
      }),
    );
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(panel.state.step).toBe("blocked");
    if (panel.state.step !== "blocked") return;
    expect(panel.state.notice.detail).toBe(testFindingSentence("agent_archived")?.detail);
    expect(panel.state.notice.detail).not.toContain("/agents/");
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
  });

  it("keeps a refusal retryable when it is not about the agent itself", async () => {
    stubFetch(409, problem(409, "This project already has a workflow with that slug"));
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(panel.state.step).toBe("refused");
    panel.send({ type: "submit" });
    expect(panel.state).toEqual({ step: "confirming" });
  });
});

/**
 * The outcome the panel used to have no name for: the action throws.
 *
 * A server action can reject rather than return - the connection drops, the
 * action id is stale after a deploy, the Next server answers 500 - and
 * neither `failure()` nor the machine sees any of it, because it never
 * reaches the `catch` inside the action. Two shapes of it are simulated
 * here, and they are chosen to sit on **opposite sides of the endpoint**:
 *
 * - `requireSession` rejecting, which throws before anything is sent;
 * - `revalidatePath` rejecting, which throws after the API has already
 *   answered 202 and the execution exists.
 *
 * The panel cannot tell them apart, which is the entire reason its sentence
 * does not claim the test did not start. The second test is the one that
 * proves the sentence has to be written that way.
 */
describe("when the action throws instead of answering", () => {
  it("leaves pending, says it does not know, and sends nothing more", async () => {
    const { calls } = stubFetch(202, ACCEPTED);
    vi.mocked(requireSession).mockRejectedValueOnce(new Error("socket hang up"));
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(panel.state).toEqual({
      step: "refused",
      message: uncertainFailureMessage(),
      field: null,
      requestId: null,
    });
    // Nothing retried on its own. The only request that could exist is the
    // one that threw, and this one threw before it was made.
    expect(calls).toHaveLength(0);
    expect(panel.guardHeld).toBe(false);
  });

  it("is still uncertain when the throw lands after the API accepted", async () => {
    // The execution exists. Had the panel said "the test did not start",
    // this is the run it would have said it about.
    const { calls } = stubFetch(202, ACCEPTED);
    vi.mocked(revalidatePath).mockImplementationOnce(() => {
      throw new Error("revalidation failed");
    });
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(calls).toHaveLength(1);
    expect(panel.state.step).toBe("refused");
    if (panel.state.step !== "refused") return;
    expect(panel.state.message).toBe(uncertainFailureMessage());
    expect(panel.state.message).not.toMatch(/did not start|was not started|no execution/i);
  });

  it("releases the in-flight guard, so a deliberate second attempt goes out", async () => {
    // **The test that fails if the `finally` is removed.** Without it the
    // guard stays latched, the second `confirm()` returns at its first line,
    // and no request is ever made - which is what the panel would do to
    // somebody who pressed the button again after a dropped connection.
    const { calls } = stubFetch(202, ACCEPTED);
    vi.mocked(requireSession).mockRejectedValueOnce(new Error("socket hang up"));
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();
    expect(panel.state.step).toBe("refused");

    panel.send({ type: "submit" });
    expect(panel.state).toEqual({ step: "confirming" });
    await panel.confirm();

    expect(calls).toHaveLength(1);
    // And the sample survived the failure intact - it is never cleared, and
    // the retry asks the agent exactly what the first attempt would have.
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ input: SAMPLE });
    expect(panel.state).toEqual({ step: "accepted", executionId: "e-1" });
  });

  it("keeps an ordinary refusal and an ordinary acceptance unchanged", async () => {
    // The `catch` is not a catch-all for the server saying no: a 403 is
    // still a refusal with the API's own sentence and its request id.
    stubFetch(403, problem(403, "This action needs the member role"));
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    await panel.confirm();

    expect(panel.state.step).toBe("refused");
    if (panel.state.step !== "refused") return;
    expect(panel.state).toEqual({
      step: "refused",
      message: "This action needs the member role",
      field: null,
      requestId: "req-42",
    });
    expect(panel.state.message).not.toBe(uncertainFailureMessage());
  });
  it("sends nothing when a cancel has already overtaken the confirmation", async () => {
    // The same regression the panel tests pin, asserted where it costs
    // money: the request must not reach the endpoint at all.
    const { calls } = stubFetch(202, ACCEPTED);
    const panel = drive(SAMPLE);

    panel.send({ type: "submit" });
    panel.send({ type: "cancel" });
    await panel.confirm();

    expect(calls).toHaveLength(0);
    expect(vi.mocked(revalidatePath)).not.toHaveBeenCalled();
    expect(panel.state).toEqual({ step: "idle" });
    expect(panel.guardHeld).toBe(false);
  });
});
