/**
 * What testing an agent means, and what a person has to be told before they
 * press it.
 *
 * The companion to `create.ts` and `edit.ts`, and here for the reason the
 * whole `lib/**` layer exists: the Vitest environment is `node`, so a
 * decision that lives in a component is a decision no test can reach. The
 * difference is what the decisions are *about*. Creating and editing are
 * forms, and their rules are about a body. A test has one field; its rules
 * are about **whether this agent can be given work at all**, **what the
 * person is agreeing to spend**, and **what the answer they get back
 * actually is**.
 *
 * Nothing here renders, and nothing here calls the API.
 *
 * ## The one thing to understand before reading on
 *
 * `POST .../test` is **asynchronous and returns an acceptance**. The
 * execution row exists when it resolves; the model has not been asked
 * anything yet. Every sentence in this file is written so that a screen
 * built from it cannot say "it worked" about a turn that has not happened -
 * which is the single easiest mistake this milestone could make, because the
 * call returns `200`-shaped success and a full execution object.
 *
 * ## Why the refusals are duplicated here
 *
 * `compile_agent_test` in the API refuses an archived, `observed` or
 * `dispatchable` agent with a finding carrying a `code`, a `detail` **and a
 * `remedy`** - and `AgentTestService._compiled_for` serialises only the
 * first two, so the remedy never leaves the server. `agentTestability` and
 * `testFindingSentence` are therefore not a convenience layer over the API's
 * words; they are the only place the remedy exists on this side. They are
 * written to agree with `apps/api/src/agentworld/domain/orchestration/
 * agent_test.py` and will drift if that file changes, which is a real cost
 * and the reason the agreement is stated here rather than implied.
 *
 * The gate is also not a boundary. The endpoint decides, every time; this
 * only keeps a refusal the UI already knows about from costing a request.
 */

import type { Sentence } from "@/lib/agents/create";
import type { Agent } from "@/lib/api/agents";
import { roleAtLeast, type MemberRole } from "@/lib/api/tenancy";

/**
 * The sample's bounds, from `AgentTestRequest`'s own schema.
 *
 * `max_length=20_000` on the API. Spelled out here so a form can stop a
 * sample that the endpoint would refuse, rather than discovering the limit
 * by being told about it after the request.
 */
export const SAMPLE_MAX = 20_000;

/**
 * Every reason this release refuses to test an agent, in the API's spelling.
 *
 * A closed set matching `AgentTestCode`, because a refusal a screen cannot
 * map onto something to do about it is a dead end for the person holding it.
 */
export type TestRefusalCode = "agent_archived" | "observed_agent" | "dispatchable_agent";

const REFUSAL_CODES: readonly TestRefusalCode[] = [
  "agent_archived",
  "observed_agent",
  "dispatchable_agent",
];

// ------------------------------------------------------------------- roles ---

/**
 * Whether this viewer may start a test.
 *
 * `agent.test` is `member` in the API's role matrix - the same threshold as
 * `agent.update`, and deliberately not the `admin` that archiving needs. A
 * function of its own rather than an alias of `canEditAgents`, because the
 * two answer different questions about different actions and a shared body
 * would quietly outlive the day the API splits them.
 *
 * Note what `member` buys: a test reaches the provider with the workspace's
 * credential and spends money. The API decided that; this reports it.
 */
export function canTestAgents(role: MemberRole): boolean {
  return roleAtLeast(role, "member");
}

/** Why a viewer has no test control, and what they can still do. */
export function cannotTestNotice(role: MemberRole): string {
  return (
    `Testing an agent needs the member role; you are a ${role}. ` +
    "You can read this agent and everything it has run."
  );
}

// ------------------------------------------------------------ testability ---

/**
 * Whether this agent can be given work at all, and why not when it cannot.
 *
 * Takes the agent and **not** the role, unlike `agentEditability` beside it.
 * The two questions are genuinely separate here: "may you test things" is
 * about the person and is `canTestAgents`, while this is a property of the
 * agent that is the same for an owner and for a viewer. Folding them would
 * make a viewer looking at an `observed` agent get the role refusal and
 * never learn the more interesting fact.
 */
export type Testability =
  | { readonly kind: "testable" }
  | { readonly kind: "refused"; readonly code: TestRefusalCode; readonly notice: Sentence };

/**
 * The compiler's own judgement, asked one step earlier.
 *
 * **The order matters and is the API's.** `_eligible` checks archived first
 * and returns immediately, so an archived `observed` agent is refused as
 * archived and says nothing about its mode. A gate here that reported the
 * mode would disagree with the 422 that follows it.
 */
export function agentTestability(
  agent: Pick<Agent, "interaction_mode" | "archived_at">,
): Testability {
  if (agent.archived_at !== null) return refused("agent_archived");
  if (agent.interaction_mode === "observed") return refused("observed_agent");
  if (agent.interaction_mode === "dispatchable") return refused("dispatchable_agent");
  return { kind: "testable" };
}

function refused(code: TestRefusalCode): Testability {
  return { kind: "refused", code, notice: refusalSentence(code) };
}

/**
 * What a refusal code means, and what to do about it.
 *
 * `null` for anything else, which is not defensiveness: `errors[]` is an
 * array the API fills from a growing set of compilers, and a screen that
 * crashed - or printed a raw code - on a finding added next release would be
 * worse than one that falls back to the API's own `detail` sentence. The
 * caller decides what to do with `null`; this only declines to guess.
 *
 * No pointer and no id ever appears in these. The API's finding points at
 * `/agents/<uuid>`, which is correct for a machine and unreadable on a
 * screen - and is exactly what `problemMessage` would otherwise paste into a
 * sentence.
 */
export function testFindingSentence(code: string): Sentence | null {
  switch (code) {
    case "agent_archived":
      return {
        headline: "This agent is archived",
        detail:
          "An archived agent is not one this project still runs, so there is nothing to " +
          "test. Restore it first, then test it.",
      };
    case "observed_agent":
      return {
        headline: "This agent only reports its own activity",
        detail:
          "An observed agent is never given work by AgentWorld, so there is nothing here " +
          "to run. Watch its reported runs instead, or change it to a managed agent.",
      };
    case "dispatchable_agent":
      return {
        headline: "This agent runs somewhere else",
        detail:
          "Pushing work to an external agent is not available yet, so AgentWorld cannot " +
          "run this one. Test it from where it runs, for now.",
      };
    default:
      return null;
  }
}

/**
 * The first refusal in a problem document's codes that this release knows.
 *
 * What a caller holding `ActionFailure.codes` actually wants: the API may
 * send several findings, and the screen needs one sentence. First rather
 * than best, because `_eligible` yields at most one of these - the order is
 * already the API's judgement and re-ranking them here would be a second
 * opinion about which refusal matters.
 */
export function testRefusal(codes: readonly string[] | undefined): Sentence | null {
  for (const code of codes ?? []) {
    const sentence = testFindingSentence(code);
    if (sentence !== null) return sentence;
  }
  return null;
}

function refusalSentence(code: TestRefusalCode): Sentence {
  const sentence = testFindingSentence(code);
  // Unreachable: `TestRefusalCode` is exactly the set the switch covers, and
  // `knownRefusalCodes` is the test that keeps the two in step.
  /* c8 ignore next */
  if (sentence === null) throw new Error(`no sentence for '${code}'`);
  return sentence;
}

/** The codes this release can explain. Exported so a test can pin the set. */
export function knownRefusalCodes(): readonly TestRefusalCode[] {
  return REFUSAL_CODES;
}

// ------------------------------------------------------------- the sample ---

export type SampleCheck =
  | { readonly ok: true; readonly sample: string }
  | { readonly ok: false; readonly field: "input"; readonly message: string };

/**
 * Whether this sample may be sent, and what is sent when it may.
 *
 * ## The decision about whitespace, stated rather than implied
 *
 * **A whitespace-only sample is refused. Everything else is sent exactly as
 * typed.**
 *
 * Those are two separate rules on purpose, and they are measured against two
 * different strings:
 *
 * - **Emptiness is judged on the trimmed sample.** `AgentTestRequest` has
 *   `min_length=1` and does not strip, so `"   "` is a request the API
 *   accepts: it publishes a harness, dispatches a turn, and asks a model to
 *   answer three spaces. That is money spent to ask nothing, which is the
 *   same reason the API refuses `""`, applied to the case its schema cannot
 *   see.
 * - **The length is judged on the sample as typed**, because that is the
 *   string the API will measure. Validating a trimmed length and sending an
 *   untrimmed one is how a form passes its own check and collects a 422.
 *   Counted in **code points**, not UTF-16 units: pydantic's `max_length`
 *   counts Python characters, so `"🙂"` is one there and two to
 *   `String.length`. Using the latter would refuse samples the API accepts -
 *   never the reverse, so this is over-strictness rather than a 422, but
 *   over-strictness a person writing in emoji or in a non-BMP script would
 *   actually hit.
 *
 * **And the sample is not normalised.** A trimmed-then-sent sample would
 * change what the model is asked - trailing newlines and leading indentation
 * are part of a prompt, and a sample ending in a newline is a different
 * question from one that does not. The sample reaches the provider as the
 * agent's one user message (rendered as `input: <sample>`), so this layer
 * has no business editing it. It decides *whether* to send, never *what*.
 */
export function sampleProblem(input: string): SampleCheck {
  if (input.trim() === "") {
    return {
      ok: false,
      field: "input",
      message:
        "A test needs something to ask. A blank sample would still run the agent and still " +
        "cost a model call.",
    };
  }
  if ([...input].length > SAMPLE_MAX) {
    return {
      ok: false,
      field: "input",
      message: `A sample may be at most ${String(SAMPLE_MAX)} characters`,
    };
  }
  return { ok: true, sample: input };
}

// --------------------------------------------------------------- the copy ---

/**
 * What starting a test actually does, for the person deciding to.
 *
 * Every clause is a fact from the endpoint rather than a hedge: it is a real
 * execution and not a preview; it uses the agent's configured model profile
 * and the workspace's credential; it is billed by the provider like any
 * other call; and it runs the agent **as configured**, which is why there is
 * nothing to override on this screen.
 *
 * What it deliberately does not say: how much it will cost. Nothing in the
 * API reports a price or an estimate, and a number invented here would be
 * the most believable wrong thing on the page.
 */
export function testWarning(): Sentence {
  return {
    headline: "A test is a real run, and it costs what a run costs",
    detail:
      "The agent is run exactly as it is configured: its own instructions, its own model " +
      "profile, and the workspace's credential for that provider. The call reaches the " +
      "provider and is billed like any other. Nothing about the agent is overridden for a " +
      "test, which is why there is nothing to change here.",
  };
}

/**
 * What the 202 means, and what it does not.
 *
 * Three facts, in the order a person needs them:
 *
 * 1. **Started, not answered.** The execution exists; the turn has not
 *    happened. `status` is `pending` or `running` and `output` is `null`.
 * 2. **Nothing on this site polls**, so the page is a snapshot and reloading
 *    is how progress is seen. Said plainly rather than hidden behind a
 *    spinner that would imply something is watching.
 * 3. **It may not settle.** A provider timeout leaves the dispatch awaiting
 *    human review, and an execution in that state stays `running` with no
 *    outcome - so no completion time is promised, here or anywhere. There is
 *    deliberately no "usually takes a few seconds" either: nothing has
 *    measured a successful turn, a provider that sends `Retry-After` is
 *    obeyed for up to fifteen minutes per retry, and a deployment with no
 *    worker running settles nothing at all.
 *
 * And what it does not claim: a test writes no agent status, no liveness and
 * no heartbeat history. It appears in the agent's executions and nowhere
 * else, which is where a reader is sent.
 */
export function testOutcomeNote(): Sentence {
  return {
    headline: "Started. The answer arrives on the execution",
    detail:
      "The run has been accepted and the agent has not answered yet. Nothing on this page " +
      "refreshes itself, so reload the execution to see where it got to. A busy provider " +
      "can hold a run for a long time, and a call that times out waits for a person - so " +
      "there is no time this is promised by. Testing does not change the agent's status " +
      "or its heartbeat; it shows up in this agent's executions.",
  };
}
