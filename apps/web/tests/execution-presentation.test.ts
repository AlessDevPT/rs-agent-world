/**
 * What an execution's steps are allowed to say on screen.
 *
 * Most of these are about **saying less than the data invites**. The API
 * narrows a step's output per kind, so `output: null` means two different
 * things - produced nothing, or its kind has no rule for showing one - and the
 * dangerous failure here is not a crash but a page that reads a `transform`'s
 * absent output as "this step did nothing". The rows and the note are tested
 * together for that reason: what is shown, and what is said about what is not.
 */

import { describe, expect, it } from "vitest";

import type { ExecutionStatus } from "@/lib/api/agents";
import type { Execution, ExecutionStep, StepKind, StepStatus } from "@/lib/api/executions";
import { pillClassName } from "@/lib/agents/presentation";
import {
  executionStatusLabel,
  executionTiming,
  initiatorSentence,
  stepKindLabel,
  stepOutputNote,
  stepOutputRows,
  stepStatusLabel,
  stepStatusTone,
  stepTiming,
  stepsEmptyState,
} from "@/lib/executions/presentation";

const EXECUTION_STATUSES: readonly ExecutionStatus[] = [
  "pending",
  "running",
  "waiting_approval",
  "waiting_input",
  "paused",
  "completed",
  "failed",
  "cancelled",
];

const STEP_STATUSES: readonly StepStatus[] = [
  "pending",
  "ready",
  "dispatched",
  "running",
  "waiting_approval",
  "succeeded",
  "failed",
  "skipped",
  "cancelled",
];

const STEP_KINDS: readonly StepKind[] = [
  "agent_task",
  "tool_call",
  "approval",
  "condition",
  "transform",
  "wait",
];

function step(overrides: Partial<ExecutionStep> = {}): ExecutionStep {
  return {
    step_key: "research",
    kind: "agent_task",
    status: "succeeded",
    attempt: 0,
    agent_key: "researcher",
    started_at: "2026-11-17T09:00:01Z",
    finished_at: "2026-11-17T09:00:03Z",
    output: null,
    error: null,
    ...overrides,
  };
}

function execution(overrides: Partial<Execution> = {}): Execution {
  return {
    id: "e-1",
    workflow_id: "wf-1",
    workflow_version_id: "v-1",
    status: "completed",
    initiator_kind: "user",
    initiator_label: "ana@example.com",
    input: {},
    output: null,
    error: null,
    created_at: "2026-11-17T09:00:00Z",
    started_at: "2026-11-17T09:00:01Z",
    finished_at: "2026-11-17T09:00:04Z",
    ...overrides,
  };
}

describe("labels", () => {
  it("names every execution status, and never leaves one blank", () => {
    for (const status of EXECUTION_STATUSES) {
      expect(executionStatusLabel(status).trim()).not.toBe("");
    }
    expect(executionStatusLabel("waiting_approval")).toBe("waiting for approval");
  });

  it("names every step status", () => {
    for (const status of STEP_STATUSES) {
      expect(stepStatusLabel(status).trim()).not.toBe("");
    }
    // "pending" is the API's word for a step the execution has not reached;
    // on screen that is what it means, and "pending" alone reads as queued.
    expect(stepStatusLabel("pending")).toBe("not reached");
  });

  it("names every step kind without an underscore in sight", () => {
    for (const kind of STEP_KINDS) {
      expect(stepKindLabel(kind)).not.toContain("_");
    }
  });
});

describe("stepStatusTone", () => {
  it("gives every status a tone the pill helper accepts", () => {
    for (const status of STEP_STATUSES) {
      expect(pillClassName(stepStatusTone(status))).toContain("pill");
    }
  });

  it("does not colour a branch not taken as a failure", () => {
    // `skipped` is a condition working, not a problem.
    expect(stepStatusTone("skipped")).toBe("neutral");
    expect(stepStatusTone("failed")).toBe("bad");
    expect(stepStatusTone("succeeded")).toBe("ok");
    expect(stepStatusTone("waiting_approval")).toBe("warn");
  });

  it("leaves a dispatched step neutral, uncertainty and all", () => {
    // A dispatch awaiting human review sits behind this status and this layer
    // cannot tell it from work in flight. Amber here would cry wolf on every
    // healthy execution.
    expect(stepStatusTone("dispatched")).toBe("neutral");
  });
});

describe("stepOutputRows", () => {
  it("shows a model turn's four fields, in reading order", () => {
    const rows = stepOutputRows(
      step({
        output: {
          text: "the answer",
          stop_reason: "completed",
          model: "a-model-id",
          usage: { input_tokens: 12, output_tokens: 3 },
        },
      }),
    );

    expect(rows).toEqual([
      { label: "Answer", value: "the answer" },
      { label: "Stopped because", value: "completed" },
      { label: "Model", value: "a-model-id" },
      { label: "Tokens", value: "12 in, 3 out" },
    ]);
  });

  it("drops a field whose type is not the one it should be", () => {
    // The API drops a mistyped field rather than correcting it, so what
    // arrives here may be missing pieces; nothing is invented to fill them.
    const rows = stepOutputRows(
      step({ output: { text: { nested: "not a string" }, model: 7, usage: "lots" } }),
    );

    expect(rows).toEqual([]);
  });

  it("treats an empty answer as no answer", () => {
    expect(stepOutputRows(step({ output: { text: "   " } }))).toEqual([]);
  });

  it("reads a condition's boolean as the branch it decided", () => {
    expect(stepOutputRows(step({ kind: "condition", output: { result: true } }))).toEqual([
      { label: "Result", value: "true - this branch was taken" },
    ]);
    expect(stepOutputRows(step({ kind: "condition", output: { result: false } }))).toEqual([
      { label: "Result", value: "false" },
    ]);
    expect(stepOutputRows(step({ kind: "condition", output: { result: "yes" } }))).toEqual([]);
  });

  it("shows nothing for a kind whose output the API does not serve", () => {
    // Belt and braces: the API already sends `null` for these, and a payload
    // arriving anyway must not be rendered on the strength of its shape.
    for (const kind of ["transform", "tool_call", "approval", "wait"] as const) {
      expect(stepOutputRows(step({ kind, output: { text: "leaked", api_key: "sk-x" } }))).toEqual(
        [],
      );
    }
  });
});

describe("stepOutputNote", () => {
  it("says why a transform shows nothing, rather than letting it read as idle", () => {
    const note = stepOutputNote(step({ kind: "transform", status: "succeeded" }));
    expect(note).toContain("copies of earlier steps");
  });

  it("says nothing when there are rows to read instead", () => {
    expect(stepOutputNote(step({ output: { text: "the answer" } }))).toBeNull();
  });

  it("distinguishes a step that produced nothing from one with no outcome yet", () => {
    expect(stepOutputNote(step({ status: "succeeded" }))).toBe(
      "Finished without producing anything to show.",
    );
    expect(stepOutputNote(step({ status: "dispatched", finished_at: null }))).toBe(
      "No outcome has been reported for this step yet.",
    );
    expect(stepOutputNote(step({ status: "running", finished_at: null }))).toBe(
      "No outcome has been reported for this step yet.",
    );
  });

  it("says nothing about approvals, which this endpoint cannot see", () => {
    // A dispatch awaiting review and a step merely in flight look identical
    // here; the review queue is a different route. The note must not pick one.
    for (const status of ["dispatched", "running"] as const) {
      const note = stepOutputNote(step({ status, finished_at: null })) ?? "";
      expect(note).not.toMatch(/approval|human|review/i);
    }
  });

  it("stays quiet for a step whose own status already explains it", () => {
    // "not reached" and "skipped" need no second sentence about output.
    expect(stepOutputNote(step({ status: "pending", started_at: null, finished_at: null }))).toBeNull();
    expect(stepOutputNote(step({ status: "skipped" }))).toBeNull();
  });

  it("does not explain the output of a failed step, which has an error to show", () => {
    expect(
      stepOutputNote(step({ status: "failed", error: { code: "x", message: "went wrong" } })),
    ).toBeNull();
  });
});

describe("timing", () => {
  it("reports a step that has not started, one in flight, and one that finished", () => {
    expect(stepTiming(step({ started_at: null, finished_at: null }))).toBe("Not started");
    expect(stepTiming(step({ finished_at: null }))).toBe("Started 2026-11-17 09:00 UTC");
    expect(stepTiming(step())).toBe("2026-11-17 09:00 UTC · took 2.0 s");
  });

  it("does not call a finished step unstarted, which is every step this release runs", () => {
    // `started_at` is folded from `workflow.step_started`, and nothing emits
    // that event: the worker reports the outcome only. So a succeeded step
    // really does arrive with no start time, and the first version of this
    // printed "Not started" beside a "succeeded" pill on a real execution.
    expect(stepTiming(step({ started_at: null }))).toBe("Finished 2026-11-17 09:00 UTC");
    expect(executionTiming(execution({ started_at: null }))).toBe(
      "Finished 2026-11-17 09:00 UTC",
    );
  });

  it("does the same for the execution, and says which of the three it is", () => {
    expect(executionTiming(execution({ started_at: null, finished_at: null }))).toBe(
      "Created 2026-11-17 09:00 UTC, not started",
    );
    expect(executionTiming(execution({ finished_at: null }))).toBe(
      "Started 2026-11-17 09:00 UTC, still running",
    );
    expect(executionTiming(execution())).toBe("2026-11-17 09:00 UTC · took 3.0 s");
  });

  it("does not turn an unparseable timestamp into a duration", () => {
    expect(stepTiming(step({ finished_at: "not a date" }))).toContain("an unrecorded time");
  });
});

describe("initiatorSentence", () => {
  it("names a person as themselves and the system as the system", () => {
    expect(initiatorSentence(execution())).toBe("Started by ana@example.com");
    expect(
      initiatorSentence(execution({ initiator_kind: "system", initiator_label: "reaper" })),
    ).toBe("Started by AgentWorld itself (reaper)");
  });
});

describe("stepsEmptyState", () => {
  it("reports an execution with no steps rather than explaining it away", () => {
    // Every published workflow has at least one step and a row is seeded per
    // step when the execution starts, so none is a fact worth escalating.
    expect(stepsEmptyState().detail).toContain("worth reporting");
  });
});
