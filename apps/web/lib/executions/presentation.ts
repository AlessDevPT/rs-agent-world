/**
 * Turning one execution and its steps into things a page can print.
 *
 * Pure, and in a `.ts` module rather than inside the page, for the reason the
 * agent presentation module gives: vitest here runs `environment: "node"` over
 * `*.test.ts` with no jsdom, so a `.tsx` page cannot be asserted on at all.
 * Anything whose correctness is worth stating belongs here, where
 * `tests/execution-presentation.test.ts` can state it.
 *
 * `Tone`, `pillClassName`, `formatTimestamp` and `formatDuration` are imported
 * from the agent module rather than copied or moved: the agent pages needed
 * them first, they are not agent-specific, and moving them now would churn a
 * file that is already shipped for no change in behaviour.
 *
 * **The hard part here is `output`, and most of this module is about saying
 * less than the data invites.** The API narrows a step's output per kind, so
 * `null` means two different things - the step produced nothing, or its kind
 * has no rule for showing one - and a page that printed "no output" for both
 * would be making a claim about a `transform` that is simply not published.
 * `stepOutputRows` shows only what is known to be a value, and
 * `stepOutputNote` says why there is nothing else.
 */

import type { ExecutionStatus } from "@/lib/api/agents";
import type { Execution, ExecutionStep, StepKind, StepStatus } from "@/lib/api/executions";
import { type Tone, formatDuration, formatTimestamp } from "@/lib/agents/presentation";

export interface Sentence {
  readonly headline: string;
  readonly detail: string;
}

export interface OutputRow {
  readonly label: string;
  readonly value: string;
}

export function executionStatusLabel(status: ExecutionStatus): string {
  switch (status) {
    case "pending":
      return "pending";
    case "running":
      return "running";
    case "waiting_approval":
      return "waiting for approval";
    case "waiting_input":
      return "waiting for input";
    case "paused":
      return "paused";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
  }
}

export function stepStatusLabel(status: StepStatus): string {
  switch (status) {
    case "pending":
      return "not reached";
    case "ready":
      return "ready";
    case "dispatched":
      return "dispatched";
    case "running":
      return "running";
    case "waiting_approval":
      return "waiting for approval";
    case "succeeded":
      return "succeeded";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    case "cancelled":
      return "cancelled";
  }
}

/**
 * The colour a step's status earns.
 *
 * `skipped` is neutral rather than bad: a branch not taken is not a failure,
 * and colouring it red would make a working condition look like a problem.
 * `dispatched` is neutral too, even though it is where a step whose dispatch
 * is awaiting review sits - this layer cannot tell that apart from work in
 * flight, and guessing amber would cry wolf on every healthy execution.
 */
export function stepStatusTone(status: StepStatus): Tone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "failed":
      return "bad";
    case "waiting_approval":
      return "warn";
    case "pending":
    case "ready":
    case "dispatched":
    case "running":
    case "skipped":
    case "cancelled":
      return "neutral";
  }
}

export function stepKindLabel(kind: StepKind): string {
  switch (kind) {
    case "agent_task":
      return "agent task";
    case "tool_call":
      return "tool call";
    case "approval":
      return "approval";
    case "condition":
      return "condition";
    case "transform":
      return "transform";
    case "wait":
      return "wait";
  }
}

/**
 * What a step's output says, as rows, and nothing beyond what it says.
 *
 * Per kind, because the API serves per kind and this mirrors it rather than
 * guessing:
 *
 * - an **agent task** carries the model's answer, why it stopped, which model
 *   answered and what it cost. Each is printed only if present, since the API
 *   drops a field whose type was wrong rather than correcting it;
 * - a **condition** carries one boolean, and "took this branch" is what that
 *   boolean means to a reader;
 * - everything else gets no rows. A `transform`'s output is never served, and
 *   a kind this release cannot run has none to serve.
 */
export function stepOutputRows(step: ExecutionStep): readonly OutputRow[] {
  const output = step.output;
  if (output === null) return [];

  if (step.kind === "condition") {
    const result = output["result"];
    if (typeof result !== "boolean") return [];
    return [{ label: "Result", value: result ? "true - this branch was taken" : "false" }];
  }

  if (step.kind !== "agent_task") return [];

  const rows: OutputRow[] = [];
  const text = output["text"];
  if (typeof text === "string" && text.trim() !== "") rows.push({ label: "Answer", value: text });
  const stopReason = output["stop_reason"];
  if (typeof stopReason === "string") rows.push({ label: "Stopped because", value: stopReason });
  const model = output["model"];
  if (typeof model === "string") rows.push({ label: "Model", value: model });
  const tokens = tokenCount(output["usage"]);
  if (tokens !== null) rows.push({ label: "Tokens", value: tokens });
  return rows;
}

/**
 * Why a step shows no output, when it shows none.
 *
 * `null` when there is nothing to explain - either rows were produced, or the
 * step has not run and its status already says so. The two cases worth a
 * sentence are the ones a reader would otherwise read as a gap in the record:
 * a transform, whose output exists and is deliberately not published, and a
 * step that finished with neither output nor error.
 */
export function stepOutputNote(step: ExecutionStep): string | null {
  if (stepOutputRows(step).length > 0) return null;

  if (step.kind === "transform") {
    return (
      "A transform's values are copies of earlier steps' output, so they are not published " +
      "here. The execution's own result is below."
    );
  }
  if (step.status === "succeeded" && step.error === null) {
    return "Finished without producing anything to show.";
  }
  if (step.status === "dispatched" || step.status === "running") {
    // Only what the status establishes. An earlier draft added "or waiting on
    // a human decision", which is true of a dispatch in `needs_review` and of
    // nothing this endpoint can see: the review queue is a different route,
    // and a step that is merely running would have been described as stuck.
    return "No outcome has been reported for this step yet.";
  }
  return null;
}

/**
 * When a step ran, as one line, with the units it earns.
 *
 * **A finished step with no start time is the ordinary case, not an anomaly.**
 * `execution_steps.started_at` is set by folding `workflow.step_started`, and
 * nothing in this release emits that event - the worker reports the outcome
 * and nothing else - so every step that has actually run has a `finished_at`
 * and no `started_at`. Saying "not started" for one of those would contradict
 * the status printed beside it, which is what the first version of this
 * function did until a real execution was put in front of it.
 */
export function stepTiming(step: ExecutionStep): string {
  if (step.started_at === null) {
    return step.finished_at === null
      ? "Not started"
      : `Finished ${formatTimestamp(step.finished_at)}`;
  }
  if (step.finished_at === null) return `Started ${formatTimestamp(step.started_at)}`;
  const elapsed = new Date(step.finished_at).getTime() - new Date(step.started_at).getTime();
  const took = Number.isNaN(elapsed) ? "an unrecorded time" : formatDuration(elapsed);
  return `${formatTimestamp(step.started_at)} · took ${took}`;
}

/** The same for the execution as a whole. */
export function executionTiming(execution: Execution): string {
  if (execution.started_at === null) {
    // The execution's own `started_at` does come from an event the
    // orchestrator emits, so this is the unusual case rather than the
    // ordinary one - but it is handled the same way, because a finish time
    // with no start is still a fact and "not started" would deny it.
    return execution.finished_at === null
      ? `Created ${formatTimestamp(execution.created_at)}, not started`
      : `Finished ${formatTimestamp(execution.finished_at)}`;
  }
  if (execution.finished_at === null) {
    return `Started ${formatTimestamp(execution.started_at)}, still running`;
  }
  const elapsed =
    new Date(execution.finished_at).getTime() - new Date(execution.started_at).getTime();
  const took = Number.isNaN(elapsed) ? "an unrecorded time" : formatDuration(elapsed);
  return `${formatTimestamp(execution.started_at)} · took ${took}`;
}

/**
 * Who started it, in words rather than a kind and a label side by side.
 *
 * `initiator_label` is written once when the execution is created and never
 * recomputed, so it survives the person being demoted or leaving - which is
 * why it is printed as it stands rather than resolved against anything.
 */
export function initiatorSentence(execution: Execution): string {
  return execution.initiator_kind === "system"
    ? `Started by AgentWorld itself (${execution.initiator_label})`
    : `Started by ${execution.initiator_label}`;
}

/** What to say when an execution has no step rows at all. */
export function stepsEmptyState(): Sentence {
  return {
    headline: "No steps recorded",
    detail:
      "Every published workflow has at least one step, and a row is created for each when " +
      "the execution starts - so an execution with none is worth reporting rather than " +
      "explaining away.",
  };
}

/** `12 in, 3 out`, or nothing if the counts are not there to print. */
function tokenCount(usage: unknown): string | null {
  if (typeof usage !== "object" || usage === null) return null;
  const counts = usage as Record<string, unknown>;
  const input = counts["input_tokens"];
  const output = counts["output_tokens"];
  const parts: string[] = [];
  if (typeof input === "number") parts.push(`${String(input)} in`);
  if (typeof output === "number") parts.push(`${String(output)} out`);
  return parts.length === 0 ? null : parts.join(", ");
}
