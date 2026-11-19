/**
 * The archived filter on the project's agent list.
 *
 * Three small decisions, kept out of the page because the page cannot be
 * asserted on here: whether the URL asked for archived agents, what the link
 * that flips it should say, and which of the two empty states is the honest
 * one afterwards.
 *
 * The default is the API's own - `include_archived=false` - so a plain URL
 * shows the project as it stands rather than as it has ever been.
 */

import { describe, expect, it } from "vitest";

import { agentsEmptyState, archivedToggle, includeArchived } from "@/lib/agents/presentation";

const AGENTS = "/w/w-1/p/p-1/agents";

describe("reading the filter from the URL", () => {
  it("includes archived agents only for archived=1", () => {
    expect(includeArchived("1")).toBe(true);
  });

  it("treats everything else as the default", () => {
    // `archived=0` and `archived=false` are people spelling "no"; a repeated
    // parameter arrives from Next as an array and is not an instruction this
    // page knows how to follow. All of them mean the API's own default.
    for (const value of [undefined, "", "0", "false", "true", "yes", ["1", "0"], ["1"]]) {
      expect(includeArchived(value)).toBe(false);
    }
  });
});

describe("the link that flips it", () => {
  it("names what the reader will get, not what they are looking at", () => {
    // A control labelled with its own current state reads as a status and
    // gets clicked by mistake.
    expect(archivedToggle(AGENTS, false)).toEqual({
      label: "Show archived too",
      href: `${AGENTS}?archived=1`,
    });
    expect(archivedToggle(AGENTS, true)).toEqual({
      label: "Hide archived",
      href: AGENTS,
    });
  });

  it("keeps the workspace and project it was given", () => {
    const other = archivedToggle("/w/w-9/p/p-9/agents", false);

    expect(other.href).toBe("/w/w-9/p/p-9/agents?archived=1");
    expect(other.href).not.toContain("w-1");
  });

  it("returns to the plain path rather than spelling out the default", () => {
    // `?archived=0` would be a URL that says what it is not; the off state
    // is the absence of the parameter.
    expect(archivedToggle(AGENTS, true).href).not.toContain("?");
  });
});

describe("the two empty states", () => {
  it("says something different depending on what was asked for", () => {
    const hiding = agentsEmptyState(false);
    const including = agentsEmptyState(true);

    expect(hiding.headline).not.toBe(including.headline);
    // Hiding them, "no agents" might be wrong - there could be archived
    // ones - so that state says they are hidden.
    expect(hiding.detail).toContain("Archived");
    // Having asked for them, "none at all" is a claim the listing supports.
    expect(including.detail).toContain("archived or otherwise");
  });
});
