/**
 * What the agent screens say, and why they say it.
 *
 * Every sentence a reader has to trust lives here rather than in a `.tsx`
 * file, for a reason that is practical rather than tidy: vitest runs
 * `environment: "node"` over `*.test.ts`, with no jsdom and no Testing
 * Library, so a claim rendered inside a component cannot be asserted on at
 * all. Keeping the judgements in a pure module is what makes them testable -
 * and these are judgements, not formatting.
 *
 * **The claim this file exists to protect.** `liveness` is a statement about
 * an agent's heartbeat and nothing else. `stale` does not mean dead,
 * disconnected or stuck: heartbeats and work are reported by different events
 * and folded into different columns, so an agent can be mid-run, reporting
 * steps every second, and still be overdue here. A UI that renders "stale" as
 * a red "not working" badge would publish a claim the data does not support,
 * which is why every liveness sentence below carries its own qualification and
 * the stale one points at where the work actually shows.
 *
 * The second claim: an external agent's **reported runs** and AgentWorld's own
 * **executions** are different records with different producers. An empty list
 * on either side is an answer about where to look, so each empty state names
 * the other section rather than apologising.
 *
 * Timestamps are formatted in UTC by hand. `toLocaleString` would render
 * differently on the server and in the browser and tear the page on hydration,
 * and a relative time computed from the browser's clock would disagree with
 * the server's own `seconds_since_heartbeat`. The only "ago" number on these
 * screens is the one the API computed.
 */

import type {
  Agent,
  AgentInteractionMode,
  AgentLiveness,
  AgentStatus,
  ExecutionStatus,
  RunStatus,
} from "@/lib/api/agents";

/**
 * How loudly to render a pill.
 *
 * `ok` and `bad` have classes in `globals.css` already; `warn` needs
 * `.pill--warn` adding when the first screen lands. `neutral` is the bare
 * `.pill`, which is the right answer for a state that is neither good nor bad
 * - most of them are.
 */
export type Tone = "ok" | "warn" | "bad" | "neutral";

export function pillClassName(tone: Tone): string {
  return tone === "neutral" ? "pill" : `pill pill--${tone}`;
}

/** A headline and the sentence that stops the headline being misread. */
export interface Sentence {
  readonly headline: string;
  readonly detail: string;
}

// ------------------------------------------------------------------ liveness ---

type HeartbeatFacts = Pick<
  Agent,
  "liveness" | "seconds_since_heartbeat" | "heartbeat_ttl_seconds"
>;

export function livenessSentence(agent: HeartbeatFacts): Sentence {
  const limit = `its limit is ${formatSeconds(agent.heartbeat_ttl_seconds)}`;
  switch (agent.liveness) {
    case "fresh":
      return {
        headline: "Heartbeat is current",
        detail: `Last heard from ${formatSilence(agent.seconds_since_heartbeat)} ago, and ${limit}.`,
      };
    case "stale":
      return {
        headline: "Heartbeat is overdue",
        detail:
          `Silent for ${formatSilence(agent.seconds_since_heartbeat)}, and ${limit}. ` +
          "This is a claim about its heartbeat and nothing else - an agent can be working " +
          "and reporting runs while it is overdue here. Reported runs is where its work shows.",
      };
    case "never_reported":
      return {
        headline: "Never sent a heartbeat",
        detail:
          "It was registered but has not been heard from yet. That is not the same as " +
          "going quiet after being seen.",
      };
    case "not_applicable":
      return {
        headline: "No heartbeat expected",
        detail:
          "AgentWorld runs this agent itself, so there is no outside worker to hear from " +
          "and no silence to measure.",
      };
  }
}

export function livenessTone(liveness: AgentLiveness): Tone {
  switch (liveness) {
    case "fresh":
      return "ok";
    case "stale":
      // Not `bad`: overdue is worth noticing and is not a failure.
      return "warn";
    case "never_reported":
    case "not_applicable":
      return "neutral";
  }
}

/** The short label for a liveness pill. Plain words, not the enum value. */
export function livenessLabel(liveness: AgentLiveness): string {
  switch (liveness) {
    case "fresh":
      return "heartbeat current";
    case "stale":
      return "heartbeat overdue";
    case "never_reported":
      return "never heard from";
    case "not_applicable":
      return "no heartbeat expected";
  }
}

// -------------------------------------------------------------------- status ---

export function statusLabel(status: AgentStatus): string {
  switch (status) {
    case "provisioning":
      return "registered, never seen";
    case "offline":
      return "offline";
    case "idle":
      return "idle";
    case "working":
      return "working";
    case "blocked":
      return "blocked";
    case "error":
      return "error";
    case "maintenance":
      return "silenced for maintenance";
    case "paused":
      return "paused";
  }
}

export function statusTone(status: AgentStatus): Tone {
  switch (status) {
    case "idle":
    case "working":
      return "ok";
    case "offline":
    case "blocked":
      return "warn";
    case "error":
      return "bad";
    case "provisioning":
    case "maintenance":
    case "paused":
      return "neutral";
  }
}

export function runStatusTone(status: RunStatus): Tone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "failed":
      return "bad";
    case "running":
    case "cancelled":
      return "neutral";
  }
}

export function executionStatusTone(status: ExecutionStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
      return "bad";
    case "waiting_approval":
    case "waiting_input":
      return "warn";
    case "pending":
    case "running":
    case "paused":
    case "cancelled":
      return "neutral";
  }
}

// --------------------------------------------------------------- empty states ---

export function statusHistoryEmptyState(mode: AgentInteractionMode): Sentence {
  if (mode === "managed") {
    return {
      headline: "No status changes reported",
      detail:
        "AgentWorld runs this agent itself, so it reports nothing of its own. What it has " +
        "done is under Executions.",
    };
  }
  return {
    headline: "No status changes yet",
    detail: "Transitions appear here as the agent reports them.",
  };
}

export function runsEmptyState(mode: AgentInteractionMode): Sentence {
  if (mode === "managed") {
    return {
      headline: "This agent reports no runs",
      detail:
        "AgentWorld runs it rather than watching it work elsewhere, so its work is under " +
        "Executions above.",
    };
  }
  return {
    headline: "No runs reported yet",
    detail: "A run appears here once the agent reports starting one.",
  };
}

export function executionsEmptyState(mode: AgentInteractionMode): Sentence {
  if (mode === "managed") {
    return {
      headline: "No executions yet",
      detail: "A test of this agent, or a workflow step assigned to it, is what puts one here.",
    };
  }
  return {
    headline: "Nothing AgentWorld ran",
    detail:
      "This agent reports its own work rather than being run by AgentWorld. Reported runs " +
      "below is its account of what it did.",
  };
}

export function agentsEmptyState(includingArchived: boolean): Sentence {
  return includingArchived
    ? {
        headline: "No agents in this project",
        detail: "Nothing has been registered here, archived or otherwise.",
      }
    : {
        headline: "No agents yet",
        detail:
          "An agent appears here once it has been registered into this project. Archived " +
          "ones are hidden.",
      };
}

// ------------------------------------------------------------------ formatting ---

/**
 * Whether a listing is standing at its cap, and should say so.
 *
 * The API has a `limit` and no cursor, so a full page is not "page one of
 * many" - it is everything the reader is going to get. Saying so is more
 * honest than a pager that cannot page.
 */
export function listingNote(shown: number, limit: number): string | null {
  return shown >= limit
    ? `Showing the most recent ${limit}. Older entries are not listed.`
    : null;
}

/** A whole number of seconds as a readable span: "45 seconds", "2 hours". */
export function formatSeconds(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return plural(whole, "second");
  if (whole < 3_600) return plural(Math.floor(whole / 60), "minute");
  if (whole < 86_400) return plural(Math.floor(whole / 3_600), "hour");
  return plural(Math.floor(whole / 86_400), "day");
}

/**
 * The same, for a figure that may be absent.
 *
 * Null means the agent has never heartbeated, which is a different statement
 * from "0 seconds" - so it never renders as a number.
 */
export function formatSilence(seconds: number | null): string {
  return seconds === null ? "an unrecorded time" : formatSeconds(seconds);
}

/** A run's duration, or what to say while it has none yet. */
export function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null) return "still running";
  const ms = Math.max(0, milliseconds);
  if (ms < 1_000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes}m ${Math.floor((ms % 60_000) / 1_000)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * An ISO timestamp as `2026-11-15 11:58 UTC`.
 *
 * UTC and hand-formatted on purpose: `toLocaleString` resolves differently on
 * the server and in the browser, and a server-rendered page that disagrees
 * with its own hydration tears. An unparseable value is returned as it came
 * rather than swallowed, so a format change upstream is visible instead of
 * silent.
 */
export function formatTimestamp(iso: string | null): string {
  if (iso === null) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (value: number): string => String(value).padStart(2, "0");
  return (
    `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())} ` +
    `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())} UTC`
  );
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
