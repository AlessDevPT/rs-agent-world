/**
 * Everything the "new agent" form decides, with no React in sight.
 *
 * The form itself is a `.tsx` client component, and vitest here runs
 * `environment: "node"` over `tests/` with no jsdom and no Testing Library -
 * so a component cannot be asserted on at all. The rules therefore live here,
 * where `tests/agent-create.test.ts` can state each one as a sentence: what
 * key the API will derive, which mode may carry a model, what the body looks
 * like, and what a refusal should be blamed on.
 *
 * **Written against the API's own behaviour, not its field descriptions.**
 * Three facts from `apps/api` shape most of what follows, and none of them is
 * visible in `AgentCreate`:
 *
 * 1. `AgentService.create` runs the key through `domain/slugs.py:validated`,
 *    which slugifies it and raises with a `slug=` context key - *not* a
 *    `field=` locator. So a bad key arrives with nothing pointing at the
 *    input, and the only way for this form to put it there is to apply the
 *    same rules before sending (`slugProblem` below is a port, and
 *    `tests/agent-create.test.ts` checks it against the Python cases).
 * 2. Only `managed` may carry a `model_profile_id`: `_check_model_mode`
 *    raises 422 and the database repeats it as a check constraint.
 * 3. `dispatch._plan` resolves nothing on the agent's behalf. A managed agent
 *    with no profile settles as `misconfigured`, and so does one whose
 *    profile has no credential. There is no fallback to the workspace
 *    default, so this form must never suggest there is one.
 */

import type { AgentInteractionMode, CreateAgentBody } from "@/lib/api/agents";
import type { ModelProfile } from "@/lib/api/model-profiles";
import { roleAtLeast, type MemberRole } from "@/lib/api/tenancy";

export interface Sentence {
  readonly headline: string;
  readonly detail: string;
}

// ------------------------------------------------------------------- roles ---

/**
 * Whether this viewer may create an agent at all.
 *
 * `agent.create` is `member` in the API's role matrix. A rendering decision
 * and not the boundary: the endpoint checks the role again, and this only
 * decides whether a form worth filling in is drawn.
 */
export function canCreateAgents(role: MemberRole): boolean {
  return roleAtLeast(role, "member");
}

/**
 * Whether this viewer may attach a model profile while creating.
 *
 * `agent.configure` is `admin`, and `AgentService.create` demands it as soon
 * as the body carries a configuration field - so a member who sent a
 * `model_profile_id` would get a 403 for the whole request rather than an
 * agent without a model. The picker is therefore not rendered for them.
 */
export function canConfigureAgents(role: MemberRole): boolean {
  return roleAtLeast(role, "admin");
}

export function cannotCreateNotice(role: MemberRole): string {
  return (
    `Creating an agent needs the member role; you are a ${role}. ` +
    "You can see the agents this project already has."
  );
}

export function configureNotice(role: MemberRole): string {
  return (
    `Attaching a model profile needs the admin role; you are a ${role}. ` +
    "You can create the agent now, and an admin can give it a model afterwards."
  );
}

// ------------------------------------------------------- interaction modes ---

export interface ModeChoice {
  readonly mode: AgentInteractionMode;
  readonly label: string;
  /** What AgentWorld does with this agent. Present tense, in this release. */
  readonly detail: string;
}

/**
 * The three modes, described by what the code does today.
 *
 * Deliberately not the enum's own comments, which describe the intent rather
 * than the release. `AgentInteractionMode.DISPATCHABLE` says "we can hand it
 * work", and `advance.py` says "pushing work to an external agent is not
 * available yet" - and `advance.py` is the one that runs. Likewise `managed`
 * promises a loop that "may really pause it"; what exists is a single turn.
 *
 * The honesty matters more here than anywhere else on the form: two of the
 * three choices produce an agent no workflow can use yet, and this build has
 * no screen for changing the mode afterwards. (The API does allow it -
 * `interaction_mode` is on `AgentUpdate` and is not one of
 * `CONFIGURATION_FIELDS`, so a `member` may PATCH it - but agent editing is
 * not part of this milestone, so the form says nothing about who could.)
 */
export function modeChoices(): readonly ModeChoice[] {
  return [
    {
      mode: "managed",
      label: "Managed — AgentWorld runs it",
      detail:
        "AgentWorld calls the model itself, using this agent's model profile and its " +
        "instructions. The only mode that can be given work in this release, and the " +
        "only one that may have a model profile.",
    },
    {
      mode: "dispatchable",
      label: "Dispatchable — you run it, it reports back",
      detail:
        "The agent runs wherever you deploy it and reports its own runs and heartbeats. " +
        "Handing work to an external agent is not available yet, so a workflow step " +
        "assigned to one is refused when the execution reaches it.",
    },
    {
      mode: "observed",
      label: "Observed — it only reports",
      detail:
        "The agent reports what it did and is never given work. That is permanent " +
        "rather than a release limit: a workflow that assigns a step to an observed " +
        "agent fails validation and cannot be published.",
    },
  ];
}

/** What this mode means for the instructions box, in one sentence. */
export function instructionsHint(mode: AgentInteractionMode): string {
  return mode === "managed"
    ? "Sent to the model as its system prompt on every turn AgentWorld runs."
    : "Stored with the agent for whoever runs it. AgentWorld does not send it anywhere " +
        "in this mode, because it does not call the model for this agent.";
}

/** What this mode means for the heartbeat, or null when it does not apply. */
export function heartbeatNote(mode: AgentInteractionMode): string | null {
  return mode === "managed"
    ? null
    : "This agent is expected to heartbeat. Until it does, it is listed as never " +
        "reported, which is a statement about the heartbeat and nothing else.";
}

// -------------------------------------------------------------------- keys ---

const KEY_MIN = 2;
const KEY_MAX = 48;
const KEY_SHAPE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const NON_ALNUM = /[^a-z0-9]+/g;

/**
 * Segments the web app uses in its own routes, refused as a key.
 *
 * A port of `RESERVED` in `apps/api/src/agentworld/domain/slugs.py`, which
 * `validated` applies to every slug it sees - an agent key included, even
 * though an agent key never appears in a URL. Kept in step with the Python
 * list rather than trimmed to the ones that could matter: the point of the
 * port is to predict the API's answer, not to improve on it.
 */
const RESERVED: ReadonlySet<string> = new Set([
  "admin",
  "api",
  "auth",
  "callback",
  "dashboard",
  "docs",
  "health",
  "healthz",
  "login",
  "logout",
  "new",
  "readyz",
  "settings",
  "signup",
  "static",
  "v1",
  "ws",
]);

/**
 * The key the API will derive from this text.
 *
 * A port of `slugify`, step for step: decompose, drop everything outside
 * ASCII, lower-case, collapse every other run into a single hyphen, trim,
 * truncate, trim again. `normalize("NFKD")` plus the non-ASCII strip is what
 * Python's `encode("ascii", "ignore")` does after the same decomposition, so
 * "Cafe Zurich" with accents yields `cafe-zurich` here too.
 *
 * The second trim is not redundant: truncation can leave a trailing hyphen,
 * and Python trims again for the same reason.
 */
export function slugify(value: string): string {
  // Spread rather than a regular expression over the code-point range: the
  // range would need a control character in its pattern, and a regex that
  // dropped the ASCII control characters instead would disagree with Python
  // about "a\nb" - which it folds to `a-b` rather than `ab`.
  const ascii = [...value.normalize("NFKD")]
    .filter((character) => (character.codePointAt(0) ?? 0) < 128)
    .join("");
  return trimHyphens(trimHyphens(ascii.toLowerCase().replace(NON_ALNUM, "-")).slice(0, KEY_MAX));
}

function trimHyphens(value: string): string {
  return value.replace(/^-+/, "").replace(/-+$/, "");
}

/**
 * Why this key is unusable, phrased the way the API phrases it.
 *
 * Returns null when it is fine. The sentences are `reason_invalid`'s, and the
 * checks run in the same order, so a key refused here and a key refused by
 * the API cannot disagree about which complaint comes first.
 */
export function slugProblem(key: string): string | null {
  if (key.length < KEY_MIN) return `must be at least ${KEY_MIN} characters`;
  if (key.length > KEY_MAX) return `must be at most ${KEY_MAX} characters`;
  if (!KEY_SHAPE.test(key)) {
    return "may contain only lowercase letters, digits and single hyphens between them";
  }
  if (RESERVED.has(key)) return "is reserved";
  return null;
}

export interface KeyPreview {
  /** What will be stored, as the API would derive it. */
  readonly key: string;
  /** Why it cannot be stored, or null. */
  readonly problem: string | null;
  /** True when nothing was typed into the key box and the name decided it. */
  readonly derived: boolean;
}

/**
 * The key this draft will get, and whether it is usable.
 *
 * Shown on the form, because the key is the identifier an external worker
 * uses to identify itself and `AgentUpdate` has no field for it at all - so
 * it cannot be changed afterwards by anybody, at any role - and because a
 * display name like "New" or "A" produces a refusal about a key nobody
 * typed. Saying what it will be is cheaper than explaining afterwards.
 */
export function keyPreview(draft: AgentDraft): KeyPreview {
  const typed = draft.agentKey.trim();
  const derived = typed === "";
  const key = slugify(derived ? draft.displayName.trim() : typed);
  return { key, problem: slugProblem(key), derived };
}

// ------------------------------------------------------------------ labels ---

export type ParsedLabels =
  | { readonly ok: true; readonly value: Readonly<Record<string, unknown>> }
  | { readonly ok: false; readonly message: string };

/**
 * The labels box, as an object or as a complaint.
 *
 * Blank means `{}`, which is also the API's default, so the field is omitted
 * from the body rather than sent empty. Anything that parses but is not a
 * plain object is refused here: `labels` is `dict[str, Any]` on the API, and
 * a list or a number would come back as a 422 about a type the person cannot
 * see.
 */
export function parseLabels(text: string): ParsedLabels {
  const trimmed = text.trim();
  if (trimmed === "") return { ok: true, value: {} };

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (error) {
    const why = error instanceof Error ? error.message : "it could not be parsed";
    return { ok: false, message: `Labels are not valid JSON: ${why}` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      message: 'Labels must be a JSON object, like {"team": "support"}',
    };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** What labels are for, said without overclaiming. */
export function labelsHint(): string {
  return (
    "Optional JSON object stored with the agent and returned by the API. " +
    "Nothing in AgentWorld reads them yet, so they are notes for your own tools."
  );
}

// ---------------------------------------------------------- model profiles ---

export interface ProfileChoice {
  readonly id: string;
  /** Name, provider and model - the three things that identify a profile. */
  readonly label: string;
  /** True when this profile cannot run yet, whatever agent names it. */
  readonly unusable: boolean;
}

/**
 * The profiles, as the select should offer them.
 *
 * Every profile the workspace has, including one with no credential: hiding
 * it would make a real row invisible, and an agent can legitimately be
 * pointed at it before the key arrives. It is marked instead, and
 * `profileWarning` says what the consequence is.
 */
export function profileChoices(profiles: readonly ModelProfile[]): readonly ProfileChoice[] {
  return profiles.map((profile) => ({
    id: profile.id,
    label: profileLabel(profile),
    unusable: profile.credential_id === null,
  }));
}

export function profileLabel(profile: ModelProfile): string {
  const suffix = profile.credential_id === null ? " — no credential" : "";
  return `${profile.name} — ${profile.provider_slug} · ${profile.model_id}${suffix}`;
}

/** What to say when the workspace has no profiles at all. */
export function profilesEmptyState(): Sentence {
  return {
    headline: "This workspace has no model profiles",
    detail:
      "A managed agent calls the provider and model named by a profile. You can create " +
      "the agent now and attach one later, but AgentWorld cannot run it until you do.",
  };
}

/**
 * What this pairing means for whether the agent can actually run.
 *
 * Three different facts, all of them from `dispatch._plan`:
 *
 * - a managed agent with no profile is a `ConflictError` at dispatch time,
 *   settled as `misconfigured`;
 * - a managed agent whose profile has no credential is the same, one step
 *   later;
 * - every other mode cannot be given work at all, so the absence of a model
 *   is not a problem and must not be reported as one.
 *
 * None of these blocks creation. They are warnings because the API allows all
 * three, and configuring an agent in stages is a normal thing to want.
 */
export function profileWarning(
  mode: AgentInteractionMode,
  profile: ModelProfile | null,
): string | null {
  if (mode !== "managed") return null;
  if (profile === null) {
    return (
      "Without a model profile, AgentWorld has nothing to call: a step assigned to this " +
      "agent settles as misconfigured. You can attach one later."
    );
  }
  if (profile.credential_id === null) {
    return (
      `'${profile.name}' has no credential, so a call made with it would have no key. ` +
      "A step assigned to this agent settles as misconfigured until the profile has one."
    );
  }
  return null;
}

/**
 * The profile that survives a change of mode.
 *
 * Only `managed` may carry one, so moving to another mode drops the
 * selection rather than sending a body the API answers with a 422 about a
 * field the person has stopped looking at. The form says so where it
 * happens; this function is the rule.
 */
export function profileAfterModeChange(selected: string, mode: AgentInteractionMode): string {
  return mode === "managed" ? selected : "";
}

// ------------------------------------------------------------------- draft ---

/** Every input on the form, as text, exactly as a controlled input holds it. */
export interface AgentDraft {
  readonly displayName: string;
  readonly agentKey: string;
  readonly mode: AgentInteractionMode;
  readonly role: string;
  readonly description: string;
  readonly instructions: string;
  readonly kind: string;
  readonly labels: string;
  /** A profile id, or "" for none. Only ever set for a managed agent. */
  readonly modelProfileId: string;
}

/**
 * A blank form.
 *
 * `managed` is preselected because it is the only mode AgentWorld can run,
 * and because the mode is always sent explicitly - the API has no default of
 * its own, so this is a starting point rather than a guess about what the
 * person meant.
 */
export const EMPTY_DRAFT: AgentDraft = {
  displayName: "",
  agentKey: "",
  mode: "managed",
  role: "",
  description: "",
  instructions: "",
  kind: "",
  labels: "",
  modelProfileId: "",
};

/** A refusal, pointed at the input that caused it where there is one. */
export interface DraftProblem {
  readonly ok: false;
  readonly message: string;
  readonly field?: string;
}

export type DraftOutcome = { readonly ok: true; readonly body: CreateAgentBody } | DraftProblem;

const NAME_MAX = 200;

/**
 * The draft as a request body, or the first reason it is not one.
 *
 * **Blank optional fields are omitted, not sent.** `role`, `description`,
 * `instructions` and `kind` are nullable columns, so sending `""` would store
 * an empty string that reads as "set to nothing" everywhere afterwards. The
 * same goes double for `agent_key`: `validated` only falls back to the
 * display name when the key is *absent*, so an empty string would be refused
 * as "must be at least 2 characters" instead of deriving the key the preview
 * promised.
 *
 * `model_profile_id` is included only for an admin creating a managed agent.
 * The other two cases are the API's two refusals - 403 for a member who sent
 * a configuration field, 422 for a mode that may not have a model - and
 * neither is worth making a person discover.
 */
export function createBody(
  draft: AgentDraft,
  options: { readonly mayConfigure: boolean },
): DraftOutcome {
  const displayName = draft.displayName.trim();
  if (displayName === "") {
    return { ok: false, message: "An agent needs a display name", field: "display_name" };
  }
  if (displayName.length > NAME_MAX) {
    return {
      ok: false,
      message: `A display name may be at most ${NAME_MAX} characters`,
      field: "display_name",
    };
  }

  const key = keyPreview({ ...draft, displayName });
  if (key.problem !== null) {
    return {
      ok: false,
      field: "agent_key",
      message: key.derived
        ? `The key derived from this name is '${key.key}', which ${key.problem}. ` +
          "Give the agent a key of its own."
        : `The key '${key.key}' ${key.problem}`,
    };
  }

  const labels = parseLabels(draft.labels);
  if (!labels.ok) return { ok: false, message: labels.message, field: "labels" };

  const typedKey = draft.agentKey.trim();
  const profileId = options.mayConfigure
    ? profileAfterModeChange(draft.modelProfileId, draft.mode)
    : "";

  return {
    ok: true,
    body: {
      display_name: displayName,
      interaction_mode: draft.mode,
      // The key is sent as the API would derive it rather than as it was
      // typed. Same answer either way - `validated` slugifies whatever it
      // gets - and it means the stored row matches the preview exactly.
      ...(typedKey === "" ? {} : { agent_key: key.key }),
      ...optional("role", draft.role),
      ...optional("description", draft.description),
      ...optional("instructions", draft.instructions),
      ...optional("kind", draft.kind),
      ...(Object.keys(labels.value).length === 0 ? {} : { labels: labels.value }),
      ...(profileId === "" ? {} : { model_profile_id: profileId }),
    },
  };
}

function optional(field: string, value: string): Record<string, string> {
  const trimmed = value.trim();
  return trimmed === "" ? {} : { [field]: trimmed };
}

// ---------------------------------------------------------------- failures ---

/**
 * Which input a refusal belongs to, when the API did not say.
 *
 * `_check_model_mode` is the only create-time error that sends a `field`
 * locator. The other two refusals a person can actually provoke arrive with
 * nothing to point at:
 *
 * - the slug 422, whose context key is `slug` and therefore never reaches the
 *   body (`LOCATOR_KEYS` is `field` and `index`);
 * - the duplicate-key 409, whose context key is `agent_key`.
 *
 * The 409 is placed on the key box by its *status*, not by reading its
 * sentence: `POST .../agents` has one unique constraint a caller can hit -
 * `agents_project_key_key` - so a conflict here is about the key whether it
 * came from the service's own check or from the database winning a race.
 *
 * **The locator is checked first, and that order matters.** One 409 from
 * this endpoint does carry one: a model profile deleted mid-request arrives
 * as `agents_model_profile_id_fkey` mapped through `CONFLICT_FOREIGN_KEYS`,
 * with `field: "model_profile_id"`. Reading the status first would file that
 * under the key the caller got right.
 *
 * The slug 422 is left where it is. A 422 without a locator could be about
 * anything the schema refused, and guessing would put a message about the
 * name under the key box.
 */
export function createFailureField(
  status: number,
  locator: string | undefined,
): string | undefined {
  if (locator !== undefined) return locator;
  return status === 409 ? "agent_key" : undefined;
}
