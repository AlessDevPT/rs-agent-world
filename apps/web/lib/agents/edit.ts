/**
 * What an edit to an agent may say, and what counts as having said it.
 *
 * The companion to `create.ts`. Creating an agent is a form that invents a
 * body from nothing; editing one starts from a row that already exists, so
 * the questions are different: what actually moved, what the API will refuse
 * before it is asked, who may send which half of the body, whether this
 * agent may be edited at all, and what archiving it would cost. Every one of
 * them is a pure function here rather than a branch in a component or a
 * server action, for the reason the whole `lib/**` layer exists: the Vitest
 * environment is `node`, so a decision that lives in a component is a
 * decision no test can reach.
 *
 * Nothing here renders, and nothing here calls the API.
 */

import { parseLabels, type Sentence } from "@/lib/agents/create";
import type { Agent, UpdateAgentBody } from "@/lib/api/agents";
import { roleAtLeast, type MemberRole } from "@/lib/api/tenancy";

/**
 * The fields this milestone lets a person change, in the API's spelling.
 *
 * Exported so a form and its tests can agree with the client about the set,
 * and read by `unsupportedField` so an action can refuse anything else.
 * What is *not* here is the point: `agent_key`, `interaction_mode`,
 * `model_overrides` and `dispatch` are all accepted by the endpoint and all
 * left out, each for a reason given on `UpdateAgentBody`.
 *
 * The last two need `admin` as well - they are the API's
 * `CONFIGURATION_FIELDS` that this milestone offers - which is
 * `CONFIGURATION_EDITS` below rather than a comment, so a form and a test
 * can ask the same question.
 */
export const EDITABLE_FIELDS = [
  "display_name",
  "role",
  "description",
  "instructions",
  "kind",
  "labels",
  "model_profile_id",
  "heartbeat_ttl_seconds",
] as const satisfies readonly (keyof UpdateAgentBody)[];

/**
 * The editable fields the API also gates behind `agent.configure`.
 *
 * Two of the four in `CONFIGURATION_FIELDS`; `model_overrides` and
 * `dispatch` are the other two and this milestone does not offer them. A
 * member who sends either of these gets a 403 for the whole request, which
 * is why the form hides them rather than letting the save fail.
 */
export const CONFIGURATION_EDITS = [
  "model_profile_id",
  "heartbeat_ttl_seconds",
] as const satisfies readonly (keyof UpdateAgentBody)[];

/**
 * The editable fields whose column is nullable, so `null` clears them.
 *
 * Taken from migration `0007_definition_core`, not from how optional the
 * field feels: `role`, `description`, `instructions` and `kind` are nullable
 * text, and `model_profile_id` is a nullable reference whose null means "no
 * model" rather than "unknown".
 */
export const CLEARABLE_FIELDS = [
  "role",
  "description",
  "instructions",
  "kind",
  "model_profile_id",
] as const satisfies readonly (keyof UpdateAgentBody)[];

/** The editable fields whose column is `NOT NULL`. A null for one is refused. */
const REQUIRED_FIELDS = [
  "display_name",
  "labels",
  "heartbeat_ttl_seconds",
] as const satisfies readonly (keyof UpdateAgentBody)[];

/**
 * The keys this body actually says something about.
 *
 * A key present with `undefined` is dropped by `JSON.stringify`, so it would
 * reach the API as no field at all. Counting it as a change would send a
 * PATCH that writes nothing and still records an `agent.update` audit row -
 * the exact request `updateAgentAction` refuses - so "did the caller change
 * anything" is asked of the values, not of `Object.keys`.
 */
export function sentFields(body: UpdateAgentBody): readonly string[] {
  return Object.entries(body)
    .filter(([, value]) => value !== undefined)
    .map(([field]) => field);
}

/**
 * The first field in this body that this milestone does not edit, if any.
 *
 * `UpdateAgentBody` leaves these out, so again no caller inside this
 * application can build one - and again a server action is reachable with
 * whatever a browser cares to post, which is the whole reason the set is a
 * value here rather than only a type.
 *
 * **Refused rather than stripped.** Quietly dropping `interaction_mode` from
 * a body that asked for it would answer "saved" to a request that was not
 * carried out, and the difference matters most exactly where this check
 * does: `model_overrides` and `dispatch` are stored by the API and read by
 * nothing, so a caller who believed they had set one would have no way to
 * discover otherwise.
 *
 * An unsupported key is refused whatever its value, including `undefined`:
 * the question is what the caller asked for, not what would survive
 * serialisation.
 */
export function unsupportedField(body: UpdateAgentBody): string | undefined {
  const allowed: readonly string[] = EDITABLE_FIELDS;
  return Object.keys(body).find((field) => !allowed.includes(field));
}

/**
 * The first `NOT NULL` field this body would clear, if any.
 *
 * `UpdateAgentBody` already forbids these nulls in the type system, so a
 * caller inside this application cannot construct one. A server action is
 * still a network boundary - it is reachable with whatever a browser cares
 * to post - and the value therefore has to be examined rather than trusted,
 * which is why the read below goes through `unknown` instead of the
 * declared type.
 *
 * The API refuses the same body with a 422 naming the same field, so this
 * changes no outcome; it saves a round trip and keeps the sentence the same
 * whichever side answers.
 */
export function unsupportedNull(body: UpdateAgentBody): string | undefined {
  const fields = body as Readonly<Record<string, unknown>>;
  for (const field of REQUIRED_FIELDS) {
    if (field in fields && fields[field] === null) return field;
  }
  return undefined;
}

/**
 * A body carrying only the fields that differ from what is stored.
 *
 * **Absent, not null**, like `profileEdits` for model profiles: a field
 * equal to the stored value is dropped from the request entirely rather than
 * sent as the value it already has. That is not tidiness. `AgentService.update`
 * records one audit row per accepted request listing the field *names* it
 * touched, so a PATCH that re-sent every field would write a history saying
 * an operator rewrote an agent's instructions when all they did was correct
 * its name.
 *
 * A `null` survives this comparison exactly when the stored value is not
 * already null - clearing a role that is set is a change; "clearing" one
 * that is already empty is not.
 *
 * `labels` is compared by value with keys sorted, so a form that reserialises
 * the same object does not report a change. Every other field is a string, an
 * id or null, where identity is the right comparison.
 *
 * The result can be empty, and an empty body is the caller's signal that
 * there is nothing to send. `updateAgentAction` refuses one rather than
 * asking the API to accept a request that writes no column.
 */
export function agentEdits(original: Agent, edits: UpdateAgentBody): UpdateAgentBody {
  const body: {
    display_name?: string;
    role?: string | null;
    description?: string | null;
    instructions?: string | null;
    kind?: string | null;
    labels?: Readonly<Record<string, unknown>>;
    model_profile_id?: string | null;
    heartbeat_ttl_seconds?: number;
  } = {};

  if (edits.display_name !== undefined && edits.display_name !== original.display_name) {
    body.display_name = edits.display_name;
  }
  if (edits.role !== undefined && edits.role !== original.role) {
    body.role = edits.role;
  }
  if (edits.description !== undefined && edits.description !== original.description) {
    body.description = edits.description;
  }
  if (edits.instructions !== undefined && edits.instructions !== original.instructions) {
    body.instructions = edits.instructions;
  }
  if (edits.kind !== undefined && edits.kind !== original.kind) {
    body.kind = edits.kind;
  }
  if (edits.labels !== undefined && !sameJson(edits.labels, original.labels)) {
    body.labels = edits.labels;
  }
  if (
    edits.model_profile_id !== undefined &&
    edits.model_profile_id !== original.model_profile_id
  ) {
    body.model_profile_id = edits.model_profile_id;
  }
  if (
    edits.heartbeat_ttl_seconds !== undefined &&
    edits.heartbeat_ttl_seconds !== original.heartbeat_ttl_seconds
  ) {
    body.heartbeat_ttl_seconds = edits.heartbeat_ttl_seconds;
  }

  return body;
}

/**
 * Whether two JSON values say the same thing, keys sorted.
 *
 * The same comparison `sameParameters` makes for a profile's parameters, and
 * written again here rather than imported: an agent's labels have nothing to
 * do with model profiles, and a shared helper would tie this module to that
 * feature's presentation layer for eight lines of recursion.
 */
function sameJson(a: unknown, b: unknown): boolean {
  return canonical(a) === canonical(b);
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, entry]) => entry !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${canonical(entry)}`).join(",")}}`;
}

// ------------------------------------------------------------------- roles ---

/**
 * Whether this viewer may edit an agent at all.
 *
 * `agent.update` is `member` in the API's role matrix - the same level as
 * creating one, and deliberately so: the fields a builder touches day to day
 * are not privileged. A rendering decision and not the boundary; the
 * endpoint checks again.
 */
export function canEditAgents(role: MemberRole): boolean {
  return roleAtLeast(role, "member");
}

/**
 * Whether this viewer may archive or restore one.
 *
 * `agent.archive` and `agent.restore` are both `admin`, unlike `agent.update`
 * beside them. The split is the API's own; this only decides whether the
 * controls are drawn.
 */
export function canArchiveAgents(role: MemberRole): boolean {
  return roleAtLeast(role, "admin");
}

export function cannotEditNotice(role: MemberRole): string {
  return (
    `Editing an agent needs the member role; you are a ${role}. ` +
    "You can read everything about this agent."
  );
}

/**
 * What to tell a member about the fields they cannot send.
 *
 * Named rather than quietly dropped: a form that simply omitted the model
 * and the heartbeat limit would leave somebody hunting for a control that is
 * not there, which is worse than a sentence saying who has it.
 */
export function configurationNotice(role: MemberRole): string {
  return (
    "The model profile and the heartbeat limit are configuration, which needs the " +
    `admin role; you are a ${role}. Everything else here you can change, and an admin ` +
    "can set those two afterwards."
  );
}

/**
 * Whether this agent can be edited at all, and if not, what to say.
 *
 * One function for a rule two screens have to agree on: the edit page draws
 * a form or a card, and the detail page draws a link or nothing, and they
 * must not disagree about which. Both reasons are refusals the API makes
 * too - a viewer's PATCH is a 403 and an archived agent's is a 409 - so this
 * decides what is worth drawing, never what is allowed.
 *
 * Role before archived state, because it is the broader answer: telling a
 * viewer that the agent is archived would imply they could edit it
 * otherwise.
 */
export type Editability =
  | { readonly kind: "editable" }
  | { readonly kind: "forbidden"; readonly notice: string }
  | { readonly kind: "archived"; readonly notice: Sentence };

export function agentEditability(
  agent: Pick<Agent, "archived_at">,
  role: MemberRole,
): Editability {
  if (!canEditAgents(role)) return { kind: "forbidden", notice: cannotEditNotice(role) };
  if (agent.archived_at !== null) return { kind: "archived", notice: archivedEditNotice() };
  return { kind: "editable" };
}

/** Why an archived agent has no form, and what to do about it. */
export function archivedEditNotice(): Sentence {
  return {
    headline: "This agent is archived",
    detail:
      "An archived agent cannot be edited: the API answers a change to one with a " +
      "conflict about its state rather than a refusal about the field. Restore it " +
      "first, then edit it.",
  };
}

// --------------------------------------------------------------- lifecycle ---

/**
 * What archiving does, in the terms a person deciding has to weigh.
 *
 * Every clause is from the backend rather than from the word "archive":
 *
 * - `facts_and_effects` excludes archived agents, so a published workflow
 *   naming one fails at decision time with `unknown_agent` - at its *next*
 *   decision, which is why an execution already in flight is included here;
 * - `AgentRepository.by_key` excludes them too, so `dispatch._plan` cannot
 *   resolve one and the step settles as misconfigured;
 * - the unique index `agents_project_key_key` spans archived rows, so the
 *   key stays taken and no new agent can be given it;
 * - nothing is deleted: runs, status history and executions stay readable.
 *
 * **It does not say which workflows are affected, because nothing can.**
 * There is no endpoint that lists the workflows naming an agent, and a
 * sentence implying this page had checked would be a claim the application
 * cannot support.
 */
export function archiveWarning(agentKey: string): Sentence {
  return {
    headline: "Archiving affects work that is already running",
    detail:
      "A workflow that names this agent fails at its next decision, and an execution " +
      "already in flight fails at its next step. AgentWorld cannot list which workflows " +
      `those are. Nothing is deleted, and the key '${agentKey}' stays reserved by this ` +
      "archived agent, so no new agent can take it. You can restore it afterwards.",
  };
}

/** What restoring does, and what it pointedly does not do. */
export function restoreNote(): string {
  return (
    "The agent comes back with the status and heartbeat it had when it was archived - " +
    "nothing is recomputed - so read its heartbeat before relying on it. Workflows that " +
    "name it can run again; none is started."
  );
}

// ------------------------------------------------------------------- draft ---

/** Every input on the edit form, as text, exactly as a controlled input holds it. */
export interface EditDraft {
  readonly displayName: string;
  readonly role: string;
  readonly description: string;
  readonly instructions: string;
  readonly kind: string;
  /** A JSON object as typed. Blank means `{}`, which is what the column holds. */
  readonly labels: string;
  /** A profile id, or "" for none. Only meaningful for a managed agent. */
  readonly modelProfileId: string;
  /** Whole seconds, as typed. */
  readonly heartbeatTtl: string;
}

/**
 * The form as the stored agent, which is what an edit form must open with.
 *
 * A null column becomes an empty box, so "cleared it" and "never touched it"
 * look the same on screen - and they *are* the same request, because
 * `agentEdits` drops a null that matches a null. Labels of `{}` open blank
 * for the same reason: `{}` in a textarea is punctuation to type around, and
 * blank parses back to `{}`.
 */
export function draftOf(agent: Agent): EditDraft {
  return {
    displayName: agent.display_name,
    role: agent.role ?? "",
    description: agent.description ?? "",
    instructions: agent.instructions ?? "",
    kind: agent.kind ?? "",
    labels: Object.keys(agent.labels).length === 0 ? "" : JSON.stringify(agent.labels, null, 2),
    modelProfileId: agent.model_profile_id ?? "",
    heartbeatTtl: String(agent.heartbeat_ttl_seconds),
  };
}

/** Whether this agent may carry a model at all. The mode is not editable here. */
export function mayHaveModel(agent: Agent): boolean {
  return agent.interaction_mode === "managed";
}

export interface EditProblem {
  readonly ok: false;
  readonly message: string;
  readonly field?: string;
}

export type EditOutcome = { readonly ok: true; readonly body: UpdateAgentBody } | EditProblem;

const NAME_MAX = 200;
/** `AgentUpdate.heartbeat_ttl_seconds` is `Field(ge=5, le=86_400)`. */
const TTL_MIN = 5;
const TTL_MAX = 86_400;
const WHOLE_NUMBER = /^[0-9]+$/;

/**
 * The draft as a PATCH body carrying only what moved, or the first reason it
 * is not one.
 *
 * **Blank clears, rather than storing an empty string.** That is the one
 * difference from `createBody`, and it follows from what the two forms are: a
 * blank box on a creation form was never filled in, so the field is omitted;
 * a blank box on an edit form is a value somebody removed, so the field is
 * `null`. The five nullable columns take it. `display_name` and `labels`
 * cannot, which is why blanking the name is refused here and blank labels
 * mean `{}`.
 *
 * **Configuration travels only for an admin.** `model_profile_id` and
 * `heartbeat_ttl_seconds` are the API's `CONFIGURATION_FIELDS`, and a member
 * who sent either would be refused the whole request - including the rename
 * they actually wanted. A member's draft therefore never carries them, and
 * the form does not draw them.
 *
 * The model travels only for a managed agent, because `_check_model_mode`
 * refuses one for any other mode and this milestone cannot change the mode.
 */
export function editBody(
  original: Agent,
  draft: EditDraft,
  options: { readonly mayConfigure: boolean },
): EditOutcome {
  const candidate: {
    display_name?: string;
    role?: string | null;
    description?: string | null;
    instructions?: string | null;
    kind?: string | null;
    labels?: Readonly<Record<string, unknown>>;
    model_profile_id?: string | null;
    heartbeat_ttl_seconds?: number;
  } = {};

  // Untouched first, trimmed second - and in that order for a reason. The
  // API's `Name` is `Annotated[str, Field(min_length=1, max_length=200)]`
  // with no `strip_whitespace`, and `role`, `description`, `instructions`
  // and `kind` are plain `str | None`, so a stored value may legitimately
  // carry surrounding whitespace. Trimming first and comparing afterwards
  // would report such a field as changed on a form nobody touched, and a
  // rename would quietly rewrite the instructions beside it - with an
  // `agent.update` audit row naming both.
  if (draft.displayName !== original.display_name) {
    const displayName = draft.displayName.trim();
    if (displayName === "") {
      return {
        ok: false,
        field: "display_name",
        message: "An agent needs a display name. It cannot be cleared.",
      };
    }
    if (displayName.length > NAME_MAX) {
      return {
        ok: false,
        field: "display_name",
        message: `A display name may be at most ${NAME_MAX} characters`,
      };
    }
    candidate.display_name = displayName;
  }

  const role = edited(draft.role, original.role);
  if (role !== undefined) candidate.role = role;
  const description = edited(draft.description, original.description);
  if (description !== undefined) candidate.description = description;
  const instructions = edited(draft.instructions, original.instructions);
  if (instructions !== undefined) candidate.instructions = instructions;
  const kind = edited(draft.kind, original.kind);
  if (kind !== undefined) candidate.kind = kind;

  const labels = parseLabels(draft.labels);
  if (!labels.ok) return { ok: false, message: labels.message, field: "labels" };
  // Always a candidate rather than conditional: `agentEdits` compares it by
  // value with keys sorted, so reserialising the same object is not a
  // change and the textarea's formatting never travels on its own.
  candidate.labels = labels.value;

  if (options.mayConfigure) {
    if (mayHaveModel(original)) {
      candidate.model_profile_id = draft.modelProfileId === "" ? null : draft.modelProfileId;
    }

    const ttl = draft.heartbeatTtl.trim();
    if (!WHOLE_NUMBER.test(ttl)) {
      return {
        ok: false,
        field: "heartbeat_ttl_seconds",
        message: "The heartbeat limit is a whole number of seconds. It cannot be cleared.",
      };
    }
    const seconds = Number(ttl);
    if (seconds < TTL_MIN || seconds > TTL_MAX) {
      return {
        ok: false,
        field: "heartbeat_ttl_seconds",
        message: `The heartbeat limit is between ${TTL_MIN} and ${TTL_MAX} seconds`,
      };
    }
    candidate.heartbeat_ttl_seconds = seconds;
  }

  const body = agentEdits(original, candidate);

  if (sentFields(body).length === 0) {
    return { ok: false, message: "Nothing has changed yet" };
  }
  return { ok: true, body };
}

/**
 * What a nullable text box is now, or `undefined` when nobody touched it.
 *
 * The raw comparison is first: a box still holding exactly what `draftOf`
 * put there has not been edited, whatever whitespace the stored value
 * carries. Once it has moved, an emptied box is a cleared value - `null`,
 * not `""` - and anything else is trimmed, because trailing whitespace
 * somebody has just typed is not content worth storing.
 */
function edited(raw: string, stored: string | null): string | null | undefined {
  if (raw === (stored ?? "")) return undefined;
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

/** What the heartbeat limit decides, for the admin changing it. */
export function heartbeatHint(agent: Agent): string {
  return mayHaveModel(agent)
    ? "How long this agent may stay silent before its heartbeat reads as stale. " +
        "AgentWorld runs a managed agent itself and it never heartbeats, so this " +
        "changes nothing you can see for this one."
    : "How long this agent may stay silent before its heartbeat reads as stale. It " +
        "changes the reading only: nothing stops, nothing restarts, and no work is " +
        "refused because of it.";
}
