/**
 * What the agent screens are allowed to claim.
 *
 * These are not formatting tests. Each one pins a statement the data either
 * supports or does not: that an overdue heartbeat is not a claim about health,
 * that a managed agent is never described as having gone quiet, and that an
 * empty list points at the other section rather than implying nothing
 * happened.
 */

import { describe, expect, it } from "vitest";

import type { AgentLiveness, AgentStatus, ExecutionStatus, RunStatus } from "@/lib/api/agents";
import {
  agentsEmptyState,
  executionStatusTone,
  executionsEmptyState,
  expectsHeartbeat,
  formatDuration,
  formatSeconds,
  formatSilence,
  formatTimestamp,
  listingNote,
  livenessLabel,
  livenessSentence,
  livenessTone,
  pillClassName,
  runStatusTone,
  runsEmptyState,
  statusHistoryEmptyState,
  statusLabel,
  statusTone,
} from "@/lib/agents/presentation";

const LIVENESS: AgentLiveness[] = ["fresh", "stale", "never_reported", "not_applicable"];

describe("the liveness sentence", () => {
  it("says something different for each of the four outcomes", () => {
    const headlines = LIVENESS.map(
      (liveness) =>
        livenessSentence({ liveness, seconds_since_heartbeat: 30, heartbeat_ttl_seconds: 60 })
          .headline,
    );
    expect(new Set(headlines).size).toBe(4);
  });

  it("qualifies an overdue heartbeat instead of calling the agent broken", () => {
    const { headline, detail } = livenessSentence({
      liveness: "stale",
      seconds_since_heartbeat: 14_400,
      heartbeat_ttl_seconds: 60,
    });

    expect(headline).toBe("Heartbeat is overdue");
    expect(detail).toContain("4 hours");
    // The TTL is configured in seconds and read in minutes; both the silence
    // and the limit roll up to the scale a person would say them at.
    expect(detail).toContain("1 minute");
    // The qualification is the point of the sentence.
    expect(detail).toContain("heartbeat and nothing else");
    expect(detail).toContain("Reported runs");
    // Claims the data does not support.
    expect(detail).not.toMatch(/dead|offline|stopped|crashed|failed/i);
  });

  it("never describes a managed agent as having gone quiet", () => {
    const { headline, detail } = livenessSentence({
      liveness: "not_applicable",
      seconds_since_heartbeat: null,
      heartbeat_ttl_seconds: 60,
    });

    expect(`${headline} ${detail}`).not.toMatch(/stale|overdue|silent|quiet/i);
    expect(detail).toContain("AgentWorld runs this agent itself");
  });

  it("distinguishes never heard from from heard from a long time ago", () => {
    const never = livenessSentence({
      liveness: "never_reported",
      seconds_since_heartbeat: null,
      heartbeat_ttl_seconds: 60,
    });

    expect(never.headline).toBe("Never sent a heartbeat");
    expect(never.detail).toContain("not the same as");
    // A null figure must never reach the page as a number or as "null".
    expect(`${never.headline} ${never.detail}`).not.toMatch(/null|NaN|0 seconds/);
  });

  it("renders a fresh heartbeat with both figures", () => {
    const fresh = livenessSentence({
      liveness: "fresh",
      seconds_since_heartbeat: 12,
      heartbeat_ttl_seconds: 60,
    });

    expect(fresh.detail).toContain("12 seconds");
    expect(fresh.detail).toContain("1 minute");
  });

  it("gives every outcome a label and a tone, and only overdue is loud", () => {
    for (const liveness of LIVENESS) {
      expect(livenessLabel(liveness)).not.toBe("");
      // The enum value itself is never shown: "not_applicable" is not English.
      expect(livenessLabel(liveness)).not.toContain("_");
    }
    expect(livenessTone("fresh")).toBe("ok");
    expect(livenessTone("stale")).toBe("warn");
    expect(livenessTone("never_reported")).toBe("neutral");
    expect(livenessTone("not_applicable")).toBe("neutral");
    // Overdue is worth noticing and is not a failure, so never `bad`.
    expect(livenessTone("stale")).not.toBe("bad");
  });

  it("expects a heartbeat from everything except an agent the API excused", () => {
    // The detail page used to decide this for itself, with
    // `interaction_mode !== "managed"` - the API's own rule, written a second
    // time. This reads the verdict instead, so the two cannot drift.
    expect(expectsHeartbeat({ liveness: "fresh" })).toBe(true);
    expect(expectsHeartbeat({ liveness: "stale" })).toBe(true);
    expect(expectsHeartbeat({ liveness: "never_reported" })).toBe(true);
    expect(expectsHeartbeat({ liveness: "not_applicable" })).toBe(false);
  });

  it("hides the heartbeat figures exactly where the sentence says there are none", () => {
    // The pairing that matters on the page: the card can never say "no
    // heartbeat expected" while a TTL and a last-heartbeat time sit under it
    // inviting the reader to conclude something is late.
    for (const liveness of LIVENESS) {
      const excused =
        livenessSentence({
          liveness,
          seconds_since_heartbeat: null,
          heartbeat_ttl_seconds: 60,
        }).headline === "No heartbeat expected";
      expect(expectsHeartbeat({ liveness })).toBe(!excused);
    }
  });
});

describe("the empty states", () => {
  it("point a managed agent's reader at its executions", () => {
    expect(runsEmptyState("managed").detail).toContain("Executions");
    expect(statusHistoryEmptyState("managed").detail).toContain("Executions");
  });

  it("point an external agent's reader at its reported runs", () => {
    for (const mode of ["dispatchable", "observed"] as const) {
      expect(executionsEmptyState(mode).detail).toContain("Reported runs");
    }
  });

  it("do not imply that nothing happened", () => {
    const sentences = [
      runsEmptyState("managed"),
      runsEmptyState("observed"),
      executionsEmptyState("managed"),
      executionsEmptyState("dispatchable"),
      statusHistoryEmptyState("managed"),
      statusHistoryEmptyState("observed"),
      agentsEmptyState(true),
      agentsEmptyState(false),
    ];
    for (const sentence of sentences) {
      expect(sentence.headline).not.toBe("");
      expect(sentence.detail).not.toBe("");
      expect(`${sentence.headline} ${sentence.detail}`).not.toMatch(/error|failed|broken/i);
    }
  });

  it("says which agents are hidden when archived ones are", () => {
    expect(agentsEmptyState(false).detail).toContain("Archived");
    expect(agentsEmptyState(true).detail).toContain("archived");
  });
});

describe("statuses", () => {
  const STATUSES: AgentStatus[] = [
    "provisioning",
    "offline",
    "idle",
    "working",
    "blocked",
    "error",
    "maintenance",
    "paused",
  ];

  it("labels every status in plain words", () => {
    for (const status of STATUSES) {
      expect(statusLabel(status)).not.toContain("_");
    }
    expect(statusLabel("provisioning")).toBe("registered, never seen");
  });

  it("reserves the loudest tone for an error", () => {
    expect(statusTone("error")).toBe("bad");
    expect(statusTone("working")).toBe("ok");
    expect(statusTone("idle")).toBe("ok");
    expect(statusTone("paused")).toBe("neutral");
    // Offline is folded by the reaper and is not a failure of the agent.
    expect(statusTone("offline")).toBe("warn");
  });

  it("tones runs and executions by outcome, not by liveness", () => {
    const runs: RunStatus[] = ["running", "succeeded", "failed", "cancelled"];
    for (const status of runs) expect(runStatusTone(status)).toBeTruthy();
    expect(runStatusTone("succeeded")).toBe("ok");
    expect(runStatusTone("failed")).toBe("bad");
    expect(runStatusTone("running")).toBe("neutral");

    const executions: ExecutionStatus[] = [
      "pending",
      "running",
      "waiting_approval",
      "waiting_input",
      "paused",
      "completed",
      "failed",
      "cancelled",
    ];
    for (const status of executions) expect(executionStatusTone(status)).toBeTruthy();
    expect(executionStatusTone("completed")).toBe("ok");
    expect(executionStatusTone("failed")).toBe("bad");
    // Waiting on a person is the one state a reader may need to act on.
    expect(executionStatusTone("waiting_approval")).toBe("warn");
    expect(executionStatusTone("cancelled")).toBe("neutral");
  });

  it("builds pill classes that exist in the stylesheet", () => {
    expect(pillClassName("neutral")).toBe("pill");
    expect(pillClassName("ok")).toBe("pill pill--ok");
    expect(pillClassName("bad")).toBe("pill pill--bad");
    // `.pill--warn` is added with the first screen; this pins the name.
    expect(pillClassName("warn")).toBe("pill pill--warn");
  });
});

describe("formatting", () => {
  it("renders a span at each scale, singular included", () => {
    expect(formatSeconds(0)).toBe("0 seconds");
    expect(formatSeconds(1)).toBe("1 second");
    expect(formatSeconds(59)).toBe("59 seconds");
    expect(formatSeconds(60)).toBe("1 minute");
    expect(formatSeconds(3_599)).toBe("59 minutes");
    expect(formatSeconds(3_600)).toBe("1 hour");
    expect(formatSeconds(86_400)).toBe("1 day");
    expect(formatSeconds(172_800)).toBe("2 days");
  });

  it("never renders a negative or fractional span", () => {
    expect(formatSeconds(-5)).toBe("0 seconds");
    expect(formatSeconds(90.7)).toBe("1 minute");
  });

  it("says an absent silence in words rather than as a number", () => {
    expect(formatSilence(null)).not.toMatch(/\d/);
    expect(formatSilence(45)).toBe("45 seconds");
  });

  it("renders a duration at each scale, and says when there is none yet", () => {
    expect(formatDuration(null)).toBe("still running");
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(950)).toBe("950 ms");
    expect(formatDuration(1_500)).toBe("1.5 s");
    expect(formatDuration(65_000)).toBe("1m 5s");
    expect(formatDuration(3_900_000)).toBe("1h 5m");
  });

  it("formats a timestamp in UTC, the same on the server and in the browser", () => {
    // `toLocaleString` would differ between the two and tear on hydration.
    expect(formatTimestamp("2026-11-15T11:58:02.123456Z")).toBe("2026-11-15 11:58 UTC");
    expect(formatTimestamp("2026-01-05T04:07:00+02:00")).toBe("2026-01-05 02:07 UTC");
    expect(formatTimestamp(null)).toBe("—");
    // An unparseable value comes back as it arrived rather than vanishing.
    expect(formatTimestamp("not a date")).toBe("not a date");
  });

  it("says a full listing is everything there is, because there is no cursor", () => {
    expect(listingNote(50, 50)).toContain("most recent 50");
    expect(listingNote(49, 50)).toBeNull();
    expect(listingNote(0, 50)).toBeNull();
  });
});
