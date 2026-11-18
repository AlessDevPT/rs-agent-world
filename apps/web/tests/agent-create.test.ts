/**
 * The rules behind the "new agent" form.
 *
 * The form is a `.tsx` client component and vitest here is node-only, so
 * nothing below renders anything. What it asserts is the layer the component
 * delegates to: the key the API will derive, which fields leave the browser,
 * which mode may carry a model, and where a refusal is pointed.
 *
 * **The slug cases are an oracle, not an opinion.** Every expectation in
 * "the key the API will derive" was produced by running
 * `agentworld.domain.slugs` over the same input, so a drift between the
 * Python and this port fails here rather than in front of a user.
 */

import { describe, expect, it } from "vitest";

import {
  EMPTY_DRAFT,
  canConfigureAgents,
  canCreateAgents,
  cannotCreateNotice,
  configureNotice,
  createBody,
  createFailureField,
  heartbeatNote,
  instructionsHint,
  keyPreview,
  labelsHint,
  modeChoices,
  parseLabels,
  profileAfterModeChange,
  profileChoices,
  profileLabel,
  profileWarning,
  profilesEmptyState,
  slugProblem,
  slugify,
  type AgentDraft,
} from "@/lib/agents/create";
import type { ModelProfile } from "@/lib/api/model-profiles";
import type { MemberRole } from "@/lib/api/tenancy";

const ADMIN = { mayConfigure: true };
const MEMBER = { mayConfigure: false };

function draft(fields: Partial<AgentDraft> = {}): AgentDraft {
  return { ...EMPTY_DRAFT, displayName: "Market Researcher", ...fields };
}

function profile(fields: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "mp-1",
    name: "Production writer",
    provider_slug: "anthropic",
    model_id: "claude-opus-5",
    parameters: {},
    credential_id: "cred-1",
    is_default: false,
    ...fields,
  };
}

// --------------------------------------------------------------------- roles ---

describe("who may do what", () => {
  it("matches the API's two different rules", () => {
    // `agent.create` is `member`; `agent.configure` is `admin`. They are
    // separate rows in the role matrix and separate checks in
    // `AgentService.create`, so they are two functions here.
    const roles: readonly MemberRole[] = ["viewer", "member", "admin", "owner"];

    expect(roles.map(canCreateAgents)).toEqual([false, true, true, true]);
    expect(roles.map(canConfigureAgents)).toEqual([false, false, true, true]);
  });

  it("names the role the reader actually has", () => {
    expect(cannotCreateNotice("viewer")).toContain("you are a viewer");
    expect(configureNotice("member")).toContain("you are a member");
  });

  it("tells a member the agent is still worth creating", () => {
    // The alternative - refusing the whole form because one field is out of
    // reach - would be worse than the truth, which is that an admin can
    // attach a model afterwards.
    expect(configureNotice("member")).toContain("an admin can give it a model afterwards");
  });
});

// --------------------------------------------------------------------- modes ---

describe("the interaction modes", () => {
  it("offers the three the API accepts, managed first", () => {
    expect(modeChoices().map((choice) => choice.mode)).toEqual([
      "managed",
      "dispatchable",
      "observed",
    ]);
  });

  it("starts on managed, the only mode AgentWorld can run", () => {
    expect(EMPTY_DRAFT.mode).toBe("managed");
  });

  it("says dispatchable work is not available yet rather than implying it is", () => {
    // `AgentCreate`'s own description says "you run it and we can hand it
    // work". `advance.py` says "pushing work to an external agent is not
    // available yet", and `advance.py` is the one that runs.
    const dispatchable = modeChoices().find((choice) => choice.mode === "dispatchable")!;
    expect(dispatchable.detail).toContain("not available yet");
  });

  it("says an observed agent is never given work, permanently", () => {
    const observed = modeChoices().find((choice) => choice.mode === "observed")!;
    expect(observed.detail).toContain("never given work");
    expect(observed.detail).toContain("permanent");
  });

  it("promises a system prompt only where one is actually sent", () => {
    // `dispatch._plan` passes `system=agent.instructions` for a managed turn
    // and there is no other reader of the column.
    expect(instructionsHint("managed")).toContain("system prompt");
    expect(instructionsHint("dispatchable")).not.toContain("system prompt");
    expect(instructionsHint("observed")).toContain("does not send it anywhere");
  });

  it("mentions the heartbeat only for a mode that has one", () => {
    // `Agent.liveness` returns `not_applicable` for a managed agent.
    expect(heartbeatNote("managed")).toBeNull();
    expect(heartbeatNote("dispatchable")).toContain("heartbeat");
    expect(heartbeatNote("observed")).toContain("heartbeat");
  });
});

// ----------------------------------------------------------------------- keys ---

describe("the key the API will derive", () => {
  const cases: ReadonlyArray<readonly [string, string, string | null]> = [
    ["Market Researcher", "market-researcher", null],
    ["  Spaced  Out  ", "spaced-out", null],
    ["Café Zürich", "cafe-zurich", null],
    ["MiXeD CaSe", "mixed-case", null],
    ["under_scores and-dashes", "under-scores-and-dashes", null],
    ["A", "a", "must be at least 2 characters"],
    ["new", "new", "is reserved"],
    ["Settings", "settings", "is reserved"],
    ["V1", "v1", "is reserved"],
    ["✨✨", "", "must be at least 2 characters"],
    ["trailing---", "trailing", null],
    ["--leading", "leading", null],
    ["a".repeat(60), "a".repeat(48), null],
    ["Agent #1 (v2)", "agent-1-v2", null],
    ["support_bot", "support-bot", null],
    ["9lives", "9lives", null],
    ["a-b", "a-b", null],
    ["a--b", "a-b", null],
    ["Ångström Unit", "angstrom-unit", null],
    ["Ops/Billing", "ops-billing", null],
    ["", "", "must be at least 2 characters"],
  ];

  it.each(cases)("slugifies %j the way Python does", (input, expected, problem) => {
    expect(slugify(input)).toBe(expected);
    expect(slugProblem(slugify(input))).toBe(problem);
  });

  it("derives from the display name when the key box is empty", () => {
    expect(keyPreview(draft())).toEqual({
      key: "market-researcher",
      problem: null,
      derived: true,
    });
  });

  it("slugifies what was typed, and says it was not derived", () => {
    expect(keyPreview(draft({ agentKey: "Research Bot" }))).toEqual({
      key: "research-bot",
      problem: null,
      derived: false,
    });
  });

  it("reports a name that cannot produce a key, blaming the key", () => {
    // "New" is a display name somebody would plausibly type, and `new` is in
    // the API's reserved list. Without this the refusal would arrive from the
    // server with nothing pointing at anything.
    const outcome = createBody(draft({ displayName: "New" }), ADMIN);

    expect(outcome.ok).toBe(false);
    expect(outcome.ok === false && outcome.field).toBe("agent_key");
    expect(outcome.ok === false && outcome.message).toContain("derived from this name");
    expect(outcome.ok === false && outcome.message).toContain("is reserved");
  });

  it("reports a typed key in its own words", () => {
    // Slugifying can only ever produce a well-shaped key, so the two ways a
    // typed one still fails are length and the reserved list - and the
    // message names the key as it would have been stored.
    expect(createBody(draft({ agentKey: "API" }), ADMIN)).toMatchObject({
      ok: false,
      field: "agent_key",
      message: "The key 'api' is reserved",
    });
    expect(createBody(draft({ agentKey: "x" }), ADMIN)).toMatchObject({
      message: "The key 'x' must be at least 2 characters",
    });
  });
});

// --------------------------------------------------------------------- labels ---

describe("labels", () => {
  it("treats blank as the API's default", () => {
    expect(parseLabels("   ")).toEqual({ ok: true, value: {} });
  });

  it("parses an object", () => {
    expect(parseLabels('{"team": "support"}')).toEqual({
      ok: true,
      value: { team: "support" },
    });
  });

  it("refuses text that is not JSON, and says what the parser said", () => {
    const parsed = parseLabels("{team: support}");
    expect(parsed.ok).toBe(false);
    expect(parsed.ok === false && parsed.message).toContain("Labels are not valid JSON");
  });

  it("refuses JSON that is not an object", () => {
    // `labels` is `dict[str, Any]` on the API, so a list would be a 422 about
    // a type the person cannot see.
    for (const text of ["[1, 2]", "42", '"text"', "null"]) {
      const parsed = parseLabels(text);
      expect(parsed.ok).toBe(false);
      expect(parsed.ok === false && parsed.message).toContain("must be a JSON object");
    }
  });

  it("does not claim anything reads them", () => {
    expect(labelsHint()).toContain("Nothing in AgentWorld reads them yet");
  });
});

// -------------------------------------------------------------- model profiles ---

describe("choosing a model profile", () => {
  it("shows the name, provider and model of each one", () => {
    expect(profileLabel(profile())).toBe("Production writer — anthropic · claude-opus-5");
  });

  it("marks a profile with no credential rather than hiding it", () => {
    // It is a real row, and pointing an agent at it before the key arrives is
    // legitimate. `profileWarning` says what the consequence is.
    expect(profileChoices([profile({ credential_id: null })])).toEqual([
      {
        id: "mp-1",
        label: "Production writer — anthropic · claude-opus-5 — no credential",
        unusable: true,
      },
    ]);
  });

  it("explains the empty case without implying a default exists", () => {
    const empty = profilesEmptyState();
    expect(empty.headline).toContain("no model profiles");
    expect(empty.detail).toContain("cannot run it until you do");
  });

  it("warns that a managed agent with no profile cannot be run", () => {
    // `dispatch._plan` raises `ConflictError` for exactly this, which the
    // worker settles as `misconfigured`. There is no fallback to the
    // workspace default.
    expect(profileWarning("managed", null)).toContain("settles as misconfigured");
  });

  it("warns that a profile without a credential cannot be run either", () => {
    expect(profileWarning("managed", profile({ credential_id: null }))).toContain(
      "has no credential",
    );
  });

  it("says nothing about a model for a mode that cannot be given work", () => {
    expect(profileWarning("dispatchable", null)).toBeNull();
    expect(profileWarning("observed", null)).toBeNull();
  });

  it("drops the selection when the mode stops being managed", () => {
    // The API answers a model on another mode with a 422 about
    // `model_profile_id`, so the form must not keep one hidden.
    expect(profileAfterModeChange("mp-1", "managed")).toBe("mp-1");
    expect(profileAfterModeChange("mp-1", "dispatchable")).toBe("");
    expect(profileAfterModeChange("mp-1", "observed")).toBe("");
  });
});

// ----------------------------------------------------------------------- body ---

describe("the create body", () => {
  it("sends the two required fields and nothing else", () => {
    const outcome = createBody(draft(), ADMIN);

    expect(outcome).toEqual({
      ok: true,
      body: { display_name: "Market Researcher", interaction_mode: "managed" },
    });
  });

  it("omits a blank key rather than sending an empty string", () => {
    // `validated` falls back to the display name only when the key is
    // *absent*. An empty string would be refused as "must be at least 2
    // characters" - a complaint about something nobody typed.
    const outcome = createBody(draft({ agentKey: "   " }), ADMIN);

    expect(outcome.ok && "agent_key" in outcome.body).toBe(false);
  });

  it("sends a typed key as the API would store it", () => {
    const outcome = createBody(draft({ agentKey: "Research Bot" }), ADMIN);

    expect(outcome.ok && outcome.body.agent_key).toBe("research-bot");
  });

  it("trims the display name", () => {
    const outcome = createBody(draft({ displayName: "  Market Researcher  " }), ADMIN);

    expect(outcome.ok && outcome.body.display_name).toBe("Market Researcher");
  });

  it("refuses a name that is only whitespace", () => {
    // `min_length=1` passes a space, and the slug derived from it does not.
    const outcome = createBody(draft({ displayName: "   " }), ADMIN);

    expect(outcome.ok).toBe(false);
    expect(outcome.ok === false && outcome.field).toBe("display_name");
  });

  it("refuses a name longer than the API's limit", () => {
    const outcome = createBody(draft({ displayName: "x".repeat(201) }), ADMIN);

    expect(outcome.ok === false && outcome.field).toBe("display_name");
  });

  it("omits every blank optional string", () => {
    // They are nullable columns. Sending "" would store an empty string that
    // reads as "set to nothing" everywhere afterwards.
    const outcome = createBody(
      draft({ role: "  ", description: "", instructions: "\n", kind: " " }),
      ADMIN,
    );

    expect(outcome.ok && outcome.body).toEqual({
      display_name: "Market Researcher",
      interaction_mode: "managed",
    });
  });

  it("carries the optional strings it was given, trimmed", () => {
    const outcome = createBody(
      draft({
        role: " Researcher ",
        description: " Watches prices ",
        instructions: " Be brief. ",
        kind: " researcher ",
      }),
      ADMIN,
    );

    expect(outcome.ok && outcome.body).toMatchObject({
      role: "Researcher",
      description: "Watches prices",
      instructions: "Be brief.",
      kind: "researcher",
    });
  });

  it("omits empty labels and sends a filled object", () => {
    expect(createBody(draft({ labels: "{}" }), ADMIN)).toEqual({
      ok: true,
      body: { display_name: "Market Researcher", interaction_mode: "managed" },
    });
    const outcome = createBody(draft({ labels: '{"team":"support"}' }), ADMIN);
    expect(outcome.ok && outcome.body.labels).toEqual({ team: "support" });
  });

  it("blames the labels box for unparseable labels", () => {
    const outcome = createBody(draft({ labels: "{" }), ADMIN);

    expect(outcome.ok === false && outcome.field).toBe("labels");
  });

  it("sends a model profile for an admin creating a managed agent", () => {
    const outcome = createBody(draft({ modelProfileId: "mp-1" }), ADMIN);

    expect(outcome.ok && outcome.body.model_profile_id).toBe("mp-1");
  });

  it("never sends a model profile for another mode", () => {
    // The API's own refusal is a 422 from `_check_model_mode`, and the
    // database repeats it as a check constraint. Neither is worth provoking.
    const outcome = createBody(draft({ mode: "observed", modelProfileId: "mp-1" }), ADMIN);

    expect(outcome.ok && "model_profile_id" in outcome.body).toBe(false);
    expect(outcome.ok && outcome.body.interaction_mode).toBe("observed");
  });

  it("never sends a model profile for a member", () => {
    // `AgentService.create` demands `agent.configure` as soon as the body
    // carries one, so a member who sent it would get a 403 for the whole
    // request rather than an agent without a model.
    const outcome = createBody(draft({ modelProfileId: "mp-1" }), MEMBER);

    expect(outcome.ok && "model_profile_id" in outcome.body).toBe(false);
    expect(outcome.ok && outcome.body.display_name).toBe("Market Researcher");
  });

  it("sends no field the API does not accept at creation", () => {
    const outcome = createBody(
      draft({
        agentKey: "research-bot",
        role: "Researcher",
        description: "d",
        instructions: "i",
        kind: "k",
        labels: '{"a":1}',
        modelProfileId: "mp-1",
      }),
      ADMIN,
    );

    // `heartbeat_ttl_seconds` is not on `AgentCreate`; `model_overrides` and
    // `dispatch` are, and nothing reads either, so they are not offered.
    expect(outcome.ok && Object.keys(outcome.body).sort()).toEqual([
      "agent_key",
      "description",
      "display_name",
      "instructions",
      "interaction_mode",
      "kind",
      "labels",
      "model_profile_id",
      "role",
    ]);
  });
});

// ------------------------------------------------------------------- failures ---

describe("where a refusal is pointed", () => {
  it("keeps the locator the API sent", () => {
    expect(createFailureField(422, "model_profile_id")).toBe("model_profile_id");
  });

  it("puts a conflict on the key box, by status rather than by sentence", () => {
    // `POST .../agents` has one unique constraint a caller can hit, so a 409
    // here is about the key whether the service's own check caught it or the
    // database won the race.
    expect(createFailureField(409, undefined)).toBe("agent_key");
  });

  it("leaves an unlocated 422 at form level", () => {
    // The slug refusal arrives this way: its context key is `slug`, which is
    // not one of the API's two locator keys. Guessing would put a message
    // about the name under the key box.
    expect(createFailureField(422, undefined)).toBeUndefined();
    expect(createFailureField(403, undefined)).toBeUndefined();
    expect(createFailureField(500, undefined)).toBeUndefined();
  });
});
