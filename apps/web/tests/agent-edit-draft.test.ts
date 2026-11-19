/**
 * What the edit form decides before it sends anything.
 *
 * The form itself is unreachable from here - vitest runs in node with no DOM -
 * so every rule it obeys lives in `lib/agents/edit.ts` and is asserted here
 * instead: what the boxes open with, what an emptied box means, which fields
 * an admin may send that a member may not, and when there is nothing to send
 * at all.
 *
 * **The one asymmetry worth stating twice.** On the creation form a blank
 * box was never filled in, so the field is omitted. On this one a blank box
 * is a value somebody removed, so the field is `null` - except for the two
 * columns that are `NOT NULL`, where blanking is refused rather than sent.
 */

import { describe, expect, it } from "vitest";

import {
  agentEditability,
  archiveWarning,
  canArchiveAgents,
  canEditAgents,
  cannotEditNotice,
  configurationNotice,
  draftOf,
  editBody,
  heartbeatHint,
  mayHaveModel,
  restoreNote,
  type EditDraft,
} from "@/lib/agents/edit";
import type { Agent } from "@/lib/api/agents";

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

const ADMIN = { mayConfigure: true };
const MEMBER = { mayConfigure: false };

function draft(patch: Partial<EditDraft> = {}): EditDraft {
  return { ...draftOf(STORED), ...patch };
}

describe("who may do what", () => {
  it("follows the API's two different levels", () => {
    // `agent.update` is member; `agent.archive` and `agent.restore` are
    // admin. Not the same question, so not the same function.
    expect(canEditAgents("viewer")).toBe(false);
    expect(canEditAgents("member")).toBe(true);
    expect(canEditAgents("admin")).toBe(true);

    expect(canArchiveAgents("member")).toBe(false);
    expect(canArchiveAgents("admin")).toBe(true);
  });

  it("names the role the reader actually has", () => {
    expect(cannotEditNotice("viewer")).toContain("viewer");
    expect(configurationNotice("member")).toContain("member");
    expect(configurationNotice("member")).toContain("admin");
  });
});

describe("opening the form", () => {
  it("shows exactly what is stored", () => {
    expect(draftOf(STORED)).toEqual({
      displayName: "Market Researcher",
      role: "Finds things",
      description: "Reads the week's filings",
      instructions: "Be brief",
      kind: "researcher",
      labels: '{\n  "team": "research"\n}',
      modelProfileId: "mp-1",
      heartbeatTtl: "60",
    });
  });

  it("opens a null column as an empty box, not as the word null", () => {
    const bare = draftOf({
      ...STORED,
      role: null,
      description: null,
      instructions: null,
      kind: null,
      model_profile_id: null,
      labels: {},
    });

    expect(bare.role).toBe("");
    expect(bare.description).toBe("");
    expect(bare.instructions).toBe("");
    expect(bare.kind).toBe("");
    expect(bare.modelProfileId).toBe("");
    // `{}` is punctuation to type around, and blank parses back to `{}`.
    expect(bare.labels).toBe("");
  });

  it("sends nothing when the form is submitted untouched", () => {
    // The round trip that matters: what the form opened with must diff to
    // nothing, or every visit would write an audit row.
    expect(editBody(STORED, draft(), ADMIN)).toEqual({
      ok: false,
      message: "Nothing has changed yet",
    });
    expect(editBody(STORED, draft(), MEMBER)).toMatchObject({ ok: false });
  });
});

describe("whether this agent can be edited at all", () => {
  it("answers about the role before the archived state", () => {
    // Telling a viewer that the agent is archived would imply they could
    // edit it otherwise.
    const archived = { archived_at: "2026-01-01T00:00:00Z" };

    expect(agentEditability(archived, "viewer")).toMatchObject({ kind: "forbidden" });
    expect(agentEditability({ archived_at: null }, "viewer")).toMatchObject({
      kind: "forbidden",
    });
  });

  it("refuses an archived agent to everybody, admins included", () => {
    // `AgentService.update` answers a write to one with a 409 about its
    // state, whatever the role, so no form is worth drawing.
    for (const role of ["member", "admin", "owner"] as const) {
      const outcome = agentEditability({ archived_at: "2026-01-01T00:00:00Z" }, role);
      expect(outcome.kind).toBe("archived");
      expect(outcome.kind === "archived" && outcome.notice.detail).toContain("Restore it");
    }
  });

  it("lets a member edit a live agent", () => {
    expect(agentEditability({ archived_at: null }, "member")).toEqual({ kind: "editable" });
    expect(agentEditability({ archived_at: null }, "admin")).toEqual({ kind: "editable" });
  });
});

describe("a stored value with whitespace around it", () => {
  // `Name` on the API is `Field(min_length=1, max_length=200)` with no
  // `strip_whitespace`, and the four nullable text columns are plain
  // `str | None`, so this is a row the API will accept and store.
  const PADDED: Agent = {
    ...STORED,
    display_name: "Market Researcher ",
    role: " Finds things",
    instructions: "Be brief. ",
    description: "Reads the week's filings  ",
    kind: " researcher ",
  };

  it("is not a change on a form nobody touched", () => {
    // Trimming before comparing would report all five as edited, so a
    // rename would quietly rewrite the instructions beside it - and the
    // audit row would name both.
    expect(editBody(PADDED, draftOf(PADDED), ADMIN)).toEqual({
      ok: false,
      message: "Nothing has changed yet",
    });
  });

  it("does not travel beside a field that really moved", () => {
    const outcome = editBody(
      PADDED,
      { ...draftOf(PADDED), role: "Analyst" },
      ADMIN,
    );

    expect(outcome).toEqual({ ok: true, body: { role: "Analyst" } });
  });

  it("is trimmed once the box is actually edited", () => {
    const outcome = editBody(
      PADDED,
      { ...draftOf(PADDED), displayName: "  Senior Researcher  " },
      ADMIN,
    );

    expect(outcome).toEqual({ ok: true, body: { display_name: "Senior Researcher" } });
  });

  it("still refuses an emptied name, and still clears an emptied role", () => {
    expect(editBody(PADDED, { ...draftOf(PADDED), displayName: "" }, ADMIN)).toMatchObject({
      ok: false,
      field: "display_name",
    });
    expect(editBody(PADDED, { ...draftOf(PADDED), role: "" }, ADMIN)).toEqual({
      ok: true,
      body: { role: null },
    });
  });
});

describe("building the patch", () => {
  it("sends only the field that moved", () => {
    const outcome = editBody(STORED, draft({ displayName: "Senior Researcher" }), ADMIN);

    expect(outcome).toEqual({ ok: true, body: { display_name: "Senior Researcher" } });
  });

  it("trims what it sends", () => {
    expect(editBody(STORED, draft({ role: "  Analyst  " }), ADMIN)).toEqual({
      ok: true,
      body: { role: "Analyst" },
    });
    // Trimmed back to what is stored is not a change.
    expect(editBody(STORED, draft({ role: " Finds things " }), ADMIN)).toMatchObject({
      ok: false,
    });
  });

  it("clears a nullable field when its box is emptied", () => {
    const outcome = editBody(
      STORED,
      draft({ role: "", description: "", instructions: "", kind: "" }),
      ADMIN,
    );

    expect(outcome).toEqual({
      ok: true,
      body: { role: null, description: null, instructions: null, kind: null },
    });
  });

  it("detaches the model profile when the select says none", () => {
    expect(editBody(STORED, draft({ modelProfileId: "" }), ADMIN)).toEqual({
      ok: true,
      body: { model_profile_id: null },
    });
  });

  it("refuses an emptied display name rather than sending null", () => {
    // `display_name` is `NOT NULL`. The API refuses the same body with a 422
    // naming the same field; this just gets there first.
    const outcome = editBody(STORED, draft({ displayName: "   " }), ADMIN);

    expect(outcome).toMatchObject({ ok: false, field: "display_name" });
    expect(outcome.ok === false && outcome.message).toContain("cannot be cleared");
  });

  it("refuses a display name longer than the column allows", () => {
    const outcome = editBody(STORED, draft({ displayName: "x".repeat(201) }), ADMIN);

    expect(outcome).toMatchObject({ ok: false, field: "display_name" });
  });

  it("treats emptied labels as {} rather than as null", () => {
    // `labels` is `NOT NULL` with a `{}` default, so "no labels" is an empty
    // object - and for an agent that already has none, that is no change.
    expect(editBody(STORED, draft({ labels: "" }), ADMIN)).toEqual({
      ok: true,
      body: { labels: {} },
    });
    expect(editBody({ ...STORED, labels: {} }, draft({ labels: "" }), ADMIN)).toMatchObject({
      ok: false,
    });
  });

  it("refuses labels that are not a JSON object", () => {
    expect(editBody(STORED, draft({ labels: "[1, 2]" }), ADMIN)).toMatchObject({
      ok: false,
      field: "labels",
    });
    expect(editBody(STORED, draft({ labels: "{oops" }), ADMIN)).toMatchObject({
      ok: false,
      field: "labels",
    });
  });

  it("does not report reformatted labels as a change", () => {
    const reordered = editBody(
      { ...STORED, labels: { team: "research", tier: "gold" } },
      draft({ labels: '{"tier":"gold","team":"research"}' }),
      ADMIN,
    );

    expect(reordered).toMatchObject({ ok: false });
  });
});

describe("configuration, which needs admin", () => {
  it("never carries the model or the heartbeat limit for a member", () => {
    // A member who sent either would get a 403 for the whole request -
    // including the rename they actually wanted.
    const outcome = editBody(
      STORED,
      draft({ displayName: "Renamed", modelProfileId: "", heartbeatTtl: "900" }),
      MEMBER,
    );

    expect(outcome).toEqual({ ok: true, body: { display_name: "Renamed" } });
  });

  it("carries both for an admin who changed them", () => {
    const outcome = editBody(
      STORED,
      draft({ modelProfileId: "mp-2", heartbeatTtl: "900" }),
      ADMIN,
    );

    expect(outcome).toEqual({
      ok: true,
      body: { model_profile_id: "mp-2", heartbeat_ttl_seconds: 900 },
    });
  });

  it("sends the heartbeat limit as a number, not as the typed string", () => {
    const outcome = editBody(STORED, draft({ heartbeatTtl: " 300 " }), ADMIN);

    expect(outcome).toEqual({ ok: true, body: { heartbeat_ttl_seconds: 300 } });
  });

  it("refuses a heartbeat limit outside the schema's own bounds", () => {
    // `Field(ge=5, le=86_400)` on `AgentUpdate`.
    for (const value of ["4", "86401"]) {
      expect(editBody(STORED, draft({ heartbeatTtl: value }), ADMIN)).toMatchObject({
        ok: false,
        field: "heartbeat_ttl_seconds",
      });
    }
    expect(editBody(STORED, draft({ heartbeatTtl: "5" }), ADMIN)).toMatchObject({ ok: true });
    expect(editBody(STORED, draft({ heartbeatTtl: "86400" }), ADMIN)).toMatchObject({
      ok: true,
    });
  });

  it("refuses a heartbeat limit that is not a whole number, or is empty", () => {
    for (const value of ["", "  ", "60.5", "-60", "sixty", "1e3"]) {
      expect(editBody(STORED, draft({ heartbeatTtl: value }), ADMIN)).toMatchObject({
        ok: false,
        field: "heartbeat_ttl_seconds",
      });
    }
  });

  it("offers a model only for a managed agent", () => {
    const observed: Agent = {
      ...STORED,
      interaction_mode: "observed",
      model_profile_id: null,
    };

    expect(mayHaveModel(STORED)).toBe(true);
    expect(mayHaveModel(observed)).toBe(false);
    // Even if a draft carried one, it never reaches the body: the API
    // refuses a model for any mode but managed, and the mode is not
    // editable here.
    expect(
      editBody(observed, { ...draftOf(observed), modelProfileId: "mp-1" }, ADMIN),
    ).toMatchObject({ ok: false });
  });
});

describe("what the lifecycle copy promises", () => {
  it("states the consequence without claiming to know which workflows", () => {
    const warning = archiveWarning("market-researcher");

    expect(warning.detail).toContain("next decision");
    expect(warning.detail).toContain("next step");
    expect(warning.detail).toContain("cannot list which workflows");
    // The unique index spans archived rows, so the key stays taken.
    expect(warning.detail).toContain("market-researcher");
    expect(warning.detail).toContain("reserved");
    // Archiving is reversible; the model-profile delete is the one that is
    // not, and only that one says so.
    expect(warning.detail).not.toContain("cannot be undone");
  });

  it("says restore brings back what was there, not something recomputed", () => {
    expect(restoreNote()).toContain("nothing is recomputed");
    expect(restoreNote()).toContain("none is started");
  });

  it("does not promise the heartbeat limit changes anything for a managed agent", () => {
    expect(heartbeatHint(STORED)).toContain("never heartbeats");
    expect(heartbeatHint({ ...STORED, interaction_mode: "dispatchable" })).toContain(
      "changes the reading only",
    );
  });
});
