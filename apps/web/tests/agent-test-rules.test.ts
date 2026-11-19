/**
 * The rules behind testing an agent: who may, which agents can be, what a
 * refusal means, and what may be sent.
 *
 * Pure functions only - nothing here touches the API, which is
 * `agents-client.test.ts`'s half, or an action, which is
 * `agent-test-actions.test.ts`'s.
 *
 * **The copy is asserted, not just called.** `testWarning` and
 * `testOutcomeNote` are the whole of what a person has to decide on before
 * spending money, and the specific failure worth guarding against is a
 * cheerful rewrite: a sentence that promises the test will finish, or that
 * forgets to say it is billed. So these tests pin the claims rather than the
 * wording - that money is mentioned, that completion is not promised, that
 * nothing says the agent's status changes.
 */

import { describe, expect, it } from "vitest";

import {
  SAMPLE_MAX,
  agentTestability,
  canTestAgents,
  cannotTestNotice,
  knownRefusalCodes,
  sampleProblem,
  testFindingSentence,
  testOutcomeNote,
  testRefusal,
  testWarning,
  type TestRefusalCode,
} from "@/lib/agents/test";
import type { Agent, AgentInteractionMode } from "@/lib/api/agents";

function agent(
  interaction_mode: AgentInteractionMode,
  archived_at: string | null = null,
): Pick<Agent, "interaction_mode" | "archived_at"> {
  return { interaction_mode, archived_at };
}

const ARCHIVED = "2026-11-18T09:00:00Z";

describe("who may start a test", () => {
  it("is member and above, which is the API's own threshold for agent.test", () => {
    expect(canTestAgents("viewer")).toBe(false);
    expect(canTestAgents("member")).toBe(true);
    expect(canTestAgents("admin")).toBe(true);
    expect(canTestAgents("owner")).toBe(true);
  });

  it("tells a viewer which role is needed and what they can still do", () => {
    const notice = cannotTestNotice("viewer");
    expect(notice).toContain("member");
    expect(notice).toContain("viewer");
    expect(notice).toMatch(/read/i);
  });
});

describe("which agents can be tested", () => {
  it("runs a managed agent that is not archived", () => {
    expect(agentTestability(agent("managed"))).toEqual({ kind: "testable" });
  });

  it("refuses an observed agent, which is never given work", () => {
    const outcome = agentTestability(agent("observed"));
    expect(outcome.kind).toBe("refused");
    if (outcome.kind !== "refused") return;
    expect(outcome.code).toBe("observed_agent");
    expect(outcome.notice.detail).toMatch(/reported runs|managed agent/i);
  });

  it("refuses a dispatchable agent, which this release cannot push work to", () => {
    const outcome = agentTestability(agent("dispatchable"));
    expect(outcome.kind).toBe("refused");
    if (outcome.kind !== "refused") return;
    expect(outcome.code).toBe("dispatchable_agent");
  });

  it("refuses an archived agent", () => {
    const outcome = agentTestability(agent("managed", ARCHIVED));
    expect(outcome.kind).toBe("refused");
    if (outcome.kind !== "refused") return;
    expect(outcome.code).toBe("agent_archived");
  });

  it("reports an archived observed agent as archived, in the API's own order", () => {
    // `_eligible` checks archived first and returns, so a gate that answered
    // "observed" here would disagree with the 422 that follows it.
    const outcome = agentTestability(agent("observed", ARCHIVED));
    expect(outcome.kind).toBe("refused");
    if (outcome.kind !== "refused") return;
    expect(outcome.code).toBe("agent_archived");
  });

  it("carries the remedy the API computes and does not send", () => {
    // Every refusal's sentence must say what to do next: the backend's
    // `remedy` field never leaves the server, so this side is the only place
    // it exists.
    for (const mode of ["observed", "dispatchable"] as const) {
      const outcome = agentTestability(agent(mode));
      if (outcome.kind !== "refused") throw new Error("expected a refusal");
      expect(outcome.notice.detail.length).toBeGreaterThan(40);
    }
    const archived = agentTestability(agent("managed", ARCHIVED));
    if (archived.kind !== "refused") throw new Error("expected a refusal");
    expect(archived.notice.detail).toMatch(/restore/i);
  });
});

describe("what a refusal code means", () => {
  it("explains every code this release can receive", () => {
    const codes: readonly TestRefusalCode[] = knownRefusalCodes();
    expect([...codes].sort()).toEqual([
      "agent_archived",
      "dispatchable_agent",
      "observed_agent",
    ]);
    for (const code of codes) {
      const sentence = testFindingSentence(code);
      expect(sentence).not.toBeNull();
      expect(sentence?.headline).toBeTruthy();
      expect(sentence?.detail).toBeTruthy();
    }
  });

  it("gives each code a different explanation", () => {
    const headlines = knownRefusalCodes().map((code) => testFindingSentence(code)?.headline);
    expect(new Set(headlines).size).toBe(3);
  });

  it("never puts a pointer or an id in a sentence", () => {
    for (const code of knownRefusalCodes()) {
      const sentence = testFindingSentence(code);
      expect(`${sentence?.headline ?? ""} ${sentence?.detail ?? ""}`).not.toMatch(
        /\/agents\/|[0-9a-f]{8}-[0-9a-f]{4}/i,
      );
    }
  });

  it("declines to guess at a code it does not know", () => {
    expect(testFindingSentence("string_too_short")).toBeNull();
    expect(testFindingSentence("")).toBeNull();
  });

  it("picks the first code it recognises out of a problem document", () => {
    expect(testRefusal(["observed_agent"])?.headline).toBe(
      testFindingSentence("observed_agent")?.headline,
    );
    expect(testRefusal(["something_new", "agent_archived"])?.headline).toBe(
      testFindingSentence("agent_archived")?.headline,
    );
    expect(testRefusal(["something_new"])).toBeNull();
    expect(testRefusal([])).toBeNull();
    expect(testRefusal(undefined)).toBeNull();
  });
});

describe("what may be sent as a sample", () => {
  it("refuses an empty sample", () => {
    const outcome = sampleProblem("");
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.field).toBe("input");
  });

  it("refuses a whitespace-only sample, which the API would accept and bill", () => {
    // The decision this module makes rather than inherits: `min_length=1`
    // does not strip, so "   " is a request that spends money asking a model
    // to answer three spaces.
    for (const blank of ["   ", "\n", "\t\t", " \n \t "]) {
      const outcome = sampleProblem(blank);
      expect(outcome.ok).toBe(false);
      if (outcome.ok) continue;
      expect(outcome.message).toMatch(/cost|blank/i);
    }
  });

  it("accepts a single character", () => {
    expect(sampleProblem("?")).toEqual({ ok: true, sample: "?" });
  });

  it("accepts a sample of exactly the maximum length", () => {
    const sample = "x".repeat(SAMPLE_MAX);
    const outcome = sampleProblem(sample);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.sample).toHaveLength(SAMPLE_MAX);
  });

  it("refuses one character more", () => {
    const outcome = sampleProblem("x".repeat(SAMPLE_MAX + 1));
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.field).toBe("input");
    expect(outcome.message).toContain(String(SAMPLE_MAX));
  });

  it("counts code points, as the API's own max_length does", () => {
    // Python counts characters; `String.length` counts UTF-16 units, so a
    // non-BMP character is one there and two here. Counting units would
    // refuse samples the API accepts.
    const emoji = "🙂".repeat(SAMPLE_MAX);
    expect(emoji.length).toBe(SAMPLE_MAX * 2);
    expect(sampleProblem(emoji).ok).toBe(true);
    expect(sampleProblem(`${emoji}🙂`).ok).toBe(false);
  });

  it("measures the length of what is sent, not of a trimmed copy", () => {
    // A form that validated a trimmed length and sent an untrimmed string
    // would pass its own check and collect a 422 from the API.
    const outcome = sampleProblem(` ${"x".repeat(SAMPLE_MAX)} `);
    expect(outcome.ok).toBe(false);
  });

  it("sends the sample exactly as typed", () => {
    // Trailing newlines and leading indentation are part of a prompt: the
    // sample reaches the provider as the agent's one user message, so
    // normalising it here would change the question being asked.
    const typed = "  Summarise this:\n\n  - one\n  - two\n";
    expect(sampleProblem(typed)).toEqual({ ok: true, sample: typed });
  });
});

describe("the copy a person decides on", () => {
  it("says a test is real, configured and billed", () => {
    const warning = testWarning();
    expect(`${warning.headline} ${warning.detail}`).toMatch(/real run/i);
    expect(warning.detail).toMatch(/billed|costs/i);
    expect(warning.detail).toMatch(/credential/i);
    expect(warning.detail).toMatch(/model profile/i);
  });

  it("does not invent a price", () => {
    expect(testWarning().detail).not.toMatch(/\$|\bcents?\b|per token/i);
  });

  it("says the run has started and has not answered", () => {
    const note = testOutcomeNote();
    expect(note.headline).toMatch(/started/i);
    expect(note.detail).toMatch(/not answered yet|has not answered/i);
  });

  it("tells the reader to reload, because nothing here polls", () => {
    expect(testOutcomeNote().detail).toMatch(/reload/i);
  });

  it("promises no completion time", () => {
    // Including no "usually a few seconds": nothing has measured a
    // successful turn, a provider's `Retry-After` is obeyed for up to
    // fifteen minutes per retry, and a deployment with no worker running
    // settles nothing at all.
    const detail = testOutcomeNote().detail;
    expect(detail).toMatch(/no time this is promised by|waits for a person/i);
    expect(detail).not.toMatch(
      /will finish|within a minute|shortly after|in seconds|usually takes|a few seconds/i,
    );
  });

  it("does not claim a test changes the agent's status or heartbeat", () => {
    const detail = testOutcomeNote().detail;
    expect(detail).toMatch(/does not change the agent's status/i);
    expect(detail).toMatch(/executions/i);
  });
});
