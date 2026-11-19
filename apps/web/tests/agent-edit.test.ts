/**
 * The two decisions an agent edit makes before anything is sent.
 *
 * `agentEdits` answers "what moved", and the answer is a PATCH body. What it
 * leaves out matters more than what it includes: `AgentService.update`
 * records one audit row per accepted request naming the fields it touched,
 * so a body that re-sent unchanged values would write a history of edits
 * nobody made.
 *
 * `unsupportedNull` answers "would the API refuse this outright", for the two
 * editable columns that are `NOT NULL`. The API does refuse them - since
 * `83ec98a`, with a 422 naming the field - so neither of these tests is
 * about inventing a rule, only about applying the existing one a step
 * earlier and spelling the same field.
 */

import { describe, expect, it } from "vitest";

import {
  CLEARABLE_FIELDS,
  CONFIGURATION_EDITS,
  EDITABLE_FIELDS,
  agentEdits,
  sentFields,
  unsupportedField,
  unsupportedNull,
} from "@/lib/agents/edit";
import type { Agent, UpdateAgentBody } from "@/lib/api/agents";

const STORED: Agent = {
  id: "a-1",
  project_id: "p-1",
  agent_key: "market-researcher",
  display_name: "Market Researcher",
  interaction_mode: "managed",
  role: "Finds things",
  description: "Reads the week's filings",
  instructions: "Be brief",
  kind: "researcher",
  labels: { team: "research" },
  model_profile_id: "mp-1",
  model_overrides: {},
  dispatch: {},
  heartbeat_ttl_seconds: 60,
  status: "idle",
  status_reason: null,
  capabilities: [],
  current_task_id: null,
  last_heartbeat_at: null,
  archived_at: null,
  liveness: "not_applicable",
  seconds_since_heartbeat: null,
};

describe("the editable set", () => {
  it("leaves out every field the endpoint takes but no screen should offer", () => {
    // `agent_key` renames an identifier other things quote; `interaction_mode`
    // changes what an agent is; `model_overrides` and `dispatch` are stored
    // and read by nothing in the dispatch path.
    const fields: readonly string[] = EDITABLE_FIELDS;
    for (const excluded of [
      "agent_key",
      "interaction_mode",
      "model_overrides",
      "dispatch",
      "status",
      "archived_at",
    ]) {
      expect(fields).not.toContain(excluded);
    }
    // `heartbeat_ttl_seconds` is the one configuration field of the four
    // that is read rather than merely stored, so it is offered - to an
    // admin, which `CONFIGURATION_EDITS` is what says.
    expect(fields).toContain("heartbeat_ttl_seconds");
    expect([...CONFIGURATION_EDITS].sort()).toEqual([
      "heartbeat_ttl_seconds",
      "model_profile_id",
    ]);
  });

  it("says a field is clearable exactly when its column is nullable", () => {
    // Migration 0007: `role`, `description`, `instructions`, `kind` and
    // `model_profile_id` are nullable; `display_name` and `labels` are not.
    expect([...CLEARABLE_FIELDS].sort()).toEqual([
      "description",
      "instructions",
      "kind",
      "model_profile_id",
      "role",
    ]);
    const clearable: readonly string[] = CLEARABLE_FIELDS;
    expect(clearable).not.toContain("display_name");
    expect(clearable).not.toContain("labels");
  });
});

describe("building the patch", () => {
  it("sends nothing at all when nothing moved", () => {
    expect(
      agentEdits(STORED, {
        display_name: "Market Researcher",
        role: "Finds things",
        description: "Reads the week's filings",
        instructions: "Be brief",
        kind: "researcher",
        labels: { team: "research" },
        model_profile_id: "mp-1",
      }),
    ).toEqual({});
  });

  it("sends only the field that changed, not the ones alongside it", () => {
    const body = agentEdits(STORED, {
      display_name: "Senior Market Researcher",
      role: "Finds things",
      instructions: "Be brief",
    });

    expect(body).toEqual({ display_name: "Senior Market Researcher" });
    expect(Object.keys(body)).toEqual(["display_name"]);
  });

  it("never invents a field the caller did not mention", () => {
    // An edit form that shows four inputs must not blank the three it does
    // not show, so an absent key stays absent rather than becoming null.
    const body = agentEdits(STORED, { role: "Analyst" });

    expect(body).toEqual({ role: "Analyst" });
    expect("description" in body).toBe(false);
    expect("instructions" in body).toBe(false);
    expect("labels" in body).toBe(false);
    expect("model_profile_id" in body).toBe(false);
  });

  it("keeps a null for a nullable field that currently holds something", () => {
    const body = agentEdits(STORED, {
      role: null,
      description: null,
      instructions: null,
      kind: null,
      model_profile_id: null,
    });

    expect(body).toEqual({
      role: null,
      description: null,
      instructions: null,
      kind: null,
      model_profile_id: null,
    });
  });

  it("drops a null for a field that is already empty", () => {
    // "Clear a role that is not set" is not a change, and sending it would
    // write an audit row saying the role was edited.
    const empty: Agent = { ...STORED, role: null, model_profile_id: null };

    expect(agentEdits(empty, { role: null, model_profile_id: null })).toEqual({});
  });

  it("compares labels by value, with keys in any order", () => {
    const reordered = agentEdits(STORED, { labels: { team: "research" } });
    const changed = agentEdits(
      { ...STORED, labels: { team: "research", tier: "gold" } },
      { labels: { tier: "gold", team: "research" } },
    );
    const different = agentEdits(STORED, { labels: { team: "support" } });

    expect(reordered).toEqual({});
    expect(changed).toEqual({});
    expect(different).toEqual({ labels: { team: "support" } });
  });

  it("treats emptying the labels object as a change, not as a null", () => {
    // `labels` is `NOT NULL` with a `{}` default, so "no labels" is an empty
    // object. A form that sent null here would be refused.
    expect(agentEdits(STORED, { labels: {} })).toEqual({ labels: {} });
  });
});

describe("the null guard", () => {
  it("names a NOT NULL field the body would clear", () => {
    // Reachable only from outside TypeScript - a posted body - which is
    // exactly why the check is here as well as in the API.
    const posted = { display_name: null } as unknown as UpdateAgentBody;

    expect(unsupportedNull(posted)).toBe("display_name");
    expect(unsupportedNull({ labels: null } as unknown as UpdateAgentBody)).toBe("labels");
  });

  it("passes a null for every field whose column is nullable", () => {
    expect(
      unsupportedNull({
        role: null,
        description: null,
        instructions: null,
        kind: null,
        model_profile_id: null,
      }),
    ).toBeUndefined();
  });

  it("says nothing about a body that clears nothing", () => {
    expect(unsupportedNull({})).toBeUndefined();
    expect(unsupportedNull({ display_name: "Researcher", labels: {} })).toBeUndefined();
  });
});

describe("the field guard", () => {
  const EXCLUDED = [
    "interaction_mode",
    "model_overrides",
    "dispatch",
    "agent_key",
    "status",
    "archived_at",
  ] as const;

  it("names any field this milestone does not edit", () => {
    for (const field of EXCLUDED) {
      const posted = { [field]: "whatever" } as unknown as UpdateAgentBody;
      expect(unsupportedField(posted)).toBe(field);
    }
  });

  it("refuses an unsupported field even alongside a legitimate one", () => {
    // The whole body is refused rather than the bad key removed: answering
    // "saved" to a request that was half carried out is the failure mode
    // this exists to prevent.
    const posted = {
      role: "Analyst",
      model_overrides: { temperature: 0.2 },
    } as unknown as UpdateAgentBody;

    expect(unsupportedField(posted)).toBe("model_overrides");
  });

  it("refuses an unsupported field whose value would not survive JSON", () => {
    const posted = { dispatch: undefined } as unknown as UpdateAgentBody;

    expect(unsupportedField(posted)).toBe("dispatch");
  });

  it("passes every field the milestone does edit", () => {
    expect(
      unsupportedField({
        display_name: "Researcher",
        role: null,
        description: null,
        instructions: null,
        kind: null,
        labels: {},
        model_profile_id: null,
      }),
    ).toBeUndefined();
    expect(unsupportedField({})).toBeUndefined();
  });
});

describe("counting what was said", () => {
  it("ignores a key whose value is undefined", () => {
    // `JSON.stringify` drops it, so it would reach the API as no field at
    // all - and an accepted PATCH that writes no column still records an
    // `agent.update` audit row.
    expect(sentFields({ role: undefined } as unknown as UpdateAgentBody)).toEqual([]);
    expect(sentFields({})).toEqual([]);
  });

  it("counts a null, which is a request to clear", () => {
    expect(sentFields({ role: null })).toEqual(["role"]);
    expect([...sentFields({ display_name: "Researcher", kind: null })].sort()).toEqual([
      "display_name",
      "kind",
    ]);
  });
});
