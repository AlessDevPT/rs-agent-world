# T1 — Secrets and envelope encryption

**Status: implemented.** Phase 3, task T1 of the staged scope agreed on 2026-11-02.
Prerequisite T0 landed in `594e160`, `b026c93`, `1d899dd` and is green in CI run #19.

T1 adds **no migration**. Migration 0007 built the `credentials` table in Phase 2 with the
columns this design needs, and nothing about the schema turned out to be wrong. What was
missing was everything above it: there was no code that could write a credential, no code
that could read one, and no keyring to read it with.

---

## 1. What was built

| | |
|---|---|
| `infra/secrets/keyring.py` | the KEK map, its parsing, and the fixed development key |
| `infra/secrets/envelope.py` | `EncryptedBlob`, the `SecretStore` protocol, AES-256-GCM over a per-row data key |
| `repositories/credentials.py` | the only module that reads the four material columns |
| `services/credentials.py` | create, rotate, revoke, delete, `rewrap_workspace`, `use` |
| `api/v1/credentials.py` | five routes, all `admin`, none that returns material |
| `settings.py` | `CREDENTIAL_KEKS`, `CREDENTIAL_ACTIVE_KEK_ID`, parsed and refused at boot |
| `domain/entities.py` | the `Credential` entity, which has no field material could occupy |
| `domain/errors.py` | `CredentialUnreadableError` — a 500, because it is this server's fault |
| `pyproject.toml` | `cryptography` named directly, and a sixth import contract |

```
secret material
   │  AES-256-GCM under a per-credential data key (DEK),
   │  AAD = "agentworld.credential.v1" ‖ workspace_id ‖ credential_id
   ▼
ciphertext + nonce                     ──► credentials.ciphertext, .nonce
   ▲
   │ wrapped by
DEK, 256-bit, one per credential
   │  AES-256-GCM under the KEK named by key_id, **same AAD**
   ▼
wrapped_key = nonce ‖ ciphertext ‖ tag ──► credentials.wrapped_key
```

---

## 2. Three places the design document was wrong

Each of these was found by writing the test the acceptance criteria asked for, not by
reading the proposal again.

### 2.1 Binding only the ciphertext leaves the obvious attack open

`01-DOMAIN-AND-PROTOCOL.md` §6 bound the **ciphertext** to
`workspace_id ‖ credential_id` and said nothing about the wrap. That stops the first thing
an attacker would try — copying `ciphertext` into their own workspace's row — and not the
second: copy `ciphertext` **and** `wrapped_key` together and the data key unwraps, because
nothing about the wrap mentioned where it lived, and then the ciphertext decrypts under a
data key that is now legitimately theirs.

Both layers carry the same AAD. The wrap refuses first.
`test_moving_both_halves_together_does_not_help` is exactly that attempt.

### 2.2 `display_hint` could not have been both derived and the documented example

The schema comment gave two examples: `"xoxb-…4f2a"` and `"ada@example.com"`. The second
could only have come from the caller, and a caller-supplied hint is a field through which
the entire secret fits into a listing endpoint — the one place it must never be. It is
derived from the material now, and `CredentialCreate` sets `extra="forbid"` so that
sending one is a 422 rather than a field a client believes it set.

Derived **only for `api_key` material**, which is the narrower rule the doc did not have.
An API key is a single opaque high-entropy token and showing its last four characters is
what the provider's own dashboard shows — which is the whole use of the hint: matching a
row here against a key listed there. The others get nothing rather than something worse:

| kind | hint | why |
|---|---|---|
| `api_key` | `…4f2a` | opaque, high entropy, matches what the provider shows |
| `basic` | none | is or contains a human-chosen password, where four characters matter |
| `oauth2` | none | the last characters belong to whichever field serialised last |
| `custom` | none | unknown by definition |

Material shorter than 20 characters gets no hint either: four characters out of twenty is
a fifth of it.

### 2.3 The routes should not be addressed by credential id

The API table wrote `DELETE /v1/credentials/{id}`. An id-addressed route has to resolve a
credential id to a workspace *before* it can authorize the caller, and that resolution is
by definition cross-tenant. `repositories/base.py` says in its own docstring that the
three unscoped lookups that exist are the entire cross-tenant surface of the system and
that a fourth would also be a change to how authentication works. This would have been a
fourth, on the table worth the most to an attacker, to save a path segment.

`/v1/api-keys/{id}` keeps the id-addressed shape, and the difference is the point: an API
key is *presented to* this server, so looking one up without knowing the tenant is
unavoidable. A credential never is.

Everything is nested under `/v1/workspaces/{ws}/credentials` instead.

---

## 3. A leak path found while testing, in code T1 did not write

The acceptance criterion "a credential appears in no log line, no event payload and no
problem document" was written about credentials. Testing it turned up the same class of
problem one level up, in `Settings`.

pydantic renders the input that failed validation into the error message. The input to
`Settings` is every environment variable the API reads — `CREDENTIAL_KEKS` and
`API_KEY_PEPPER` among them. So the one error nobody can suppress, on the one path that
cannot be retried, printed key material:

```
1 validation error for Settings
  Value error, production requires these to be set: … [type=value_error,
  input_value={'environment': 'production', …, 'credential_keks': '{"k2026a":"…"}'}]
```

A boot failure is the message most likely to be copied into a terminal, pasted into a
ticket, or shipped to a log aggregator by a container runtime. `Settings` now sets
`hide_input_in_errors=True`, and `test_a_boot_failure_does_not_print_the_key_material`
keeps it set. The field names are still named, which is all an operator needs in order to
fix it.

This predates T1 — `API_KEY_PEPPER` has been in that message since Phase 1.

---

## 4. Acceptance criteria

From `08-PHASE-3-READINESS.md` §T1, each with the test that exercises it.

| Criterion | Test | |
|---|---|---|
| A credential row copied into another workspace **cannot be decrypted** — the AAD mismatch is the failure, not a service check | `test_a_row_copied_into_another_workspace_cannot_be_decrypted`, `test_the_same_row_id_in_the_wrong_workspace_is_also_refused` | ✅ |
| KEK rotation re-wraps every DEK with no plaintext leaving the store and **no change to any `ciphertext`** | `test_rotating_the_kek_rewraps_every_key_and_changes_no_ciphertext` | ✅ |
| No endpoint returns a secret. `display_hint` is the only hint, and it is derived | `test_no_credential_schema_has_a_property_for_material`, `test_there_is_no_route_that_reads_a_credential`, `test_the_hint_is_derived_and_cannot_be_supplied` | ✅ |
| A credential appears in no log line, no event payload and no problem document | `test_the_material_reaches_no_response_log_or_audit_row` | ✅ |
| Starting the app in `production` with no `CREDENTIAL_KEKS` fails at boot | `test_production_refuses_to_boot_without_secrets`, `test_production_refuses_a_half_configured_keyring` | ✅ |

The first criterion is the one worth reading the test for. It does not ask a service to
refuse: it copies the row **as the schema owner**, below row-level security, below the
repository's tenant scope and below `authorize` — every control the application has — and
then reads it through the service in its new home as a caller who is legitimately an admin
there. It fails in the cipher. The same test reads the original row successfully first, so
the failure is a statement about the AAD rather than about a broken fixture. It takes two
tests rather than one, for the reason in §5.1.

The fourth is checked in four places rather than one: the response bodies the server sent
across four different failure paths, the `audit_log` metadata it wrote, the `events`
payloads it appended, and the lines it printed to stdout. Each of the three greps has a
guard above it asserting the thing it greps is non-empty, because a grep over nothing
passes.

---

## 5. Non-vacuity

Every guard weakened in turn, to find out which test notices. Three of the nine told me
something I had got wrong, and all three are in the table.

| Weakened | What failed |
|---|---|
| `workspace_id` dropped from `_aad` | `test_the_same_row_id_in_the_wrong_workspace_is_also_refused`, `test_a_row_read_for_another_workspace_cannot_be_decrypted`, `test_a_rewrapped_row_is_still_bound_to_its_workspace`, `test_the_failure_message_says_nothing_about_the_cause` |
| wrap AAD replaced with `b""`, both sides | `test_a_row_read_for_another_workspace_cannot_be_decrypted`, `test_moving_both_halves_together_does_not_help` |
| `rewrap` returning a fresh `nonce` | `test_rewrapping_moves_the_key_and_leaves_the_ciphertext_alone` |
| `CredentialRepository.rewrap` also writing `nonce` | `test_rotating_the_kek_rewraps_every_key_and_changes_no_ciphertext` |
| `extra="forbid"` removed from `CredentialCreate` | `test_the_hint_is_derived_and_cannot_be_supplied` |
| `_display_hint` kind check removed | `test_only_api_key_material_earns_a_hint` (all three kinds) |
| `CREDENTIAL_KEKS` removed from the `missing` list | `test_production_refuses_to_boot_without_secrets` |
| `hide_input_in_errors` removed | `test_a_boot_failure_does_not_print_the_key_material` |
| `material()` no longer filtering `revoked_at` | `test_revoking_stops_use_but_keeps_the_material` |
| `erase()` no longer zeroing the columns | `test_deleting_destroys_the_material_and_keeps_the_row` |

### 5.1 The copy test does not test what its name suggests

Dropping `workspace_id` from the AAD does **not** fail
`test_a_row_copied_into_another_workspace_cannot_be_decrypted`. That test gives the copied
row a new id, so the `credential_id` half of the binding refuses it on its own — it would
pass with the workspace binding entirely absent.

The test that isolates the workspace component is
`test_the_same_row_id_in_the_wrong_workspace_is_also_refused`, which moves the row and
keeps its id. Both are needed and neither is redundant: the first is the realistic attack
(`INSERT ... SELECT` into your own workspace, which has to produce a new primary key), the
second is the one that proves *which* field is doing the work. Without the second, the
`workspace_id` term in the AAD was decoration.

### 5.2 The wrap's binding had exactly one test, and not the one named after it

With the AAD removed from the wrap on both sides, the only failure was
`test_a_row_read_for_another_workspace_cannot_be_decrypted` — because it asserts
`reason == "wrapped_key_tag_mismatch"`. Everything else still refused, one layer later, in
the ciphertext.

`test_moving_both_halves_together_does_not_help` passed with the wrap binding gone, which
made it a second test of the ciphertext binding wearing the name of the wrap's. It now
asserts the reason too. This is the whole value of the exercise: the test existed, was
named for the right property, and was checking a different one.

### 5.3 "A rotation cannot change the ciphertext" has two independent guards

Making `rewrap` return a fresh nonce changes nothing end to end, because
`CredentialRepository.rewrap` names two columns — `wrapped_key` and `key_id` — and the
nonce it was handed never reaches the database. So the integration test cannot see that
weakening, and the unit test can; weakening the SQL instead is the reverse. Both were
checked separately, and both halves are covered. Two guards, two tests, neither covering
the other.

## 6. What T1 deliberately does not do

- **No KMS backend.** `SecretStore` is a Protocol with three methods and the envelope
  implementation behind it; a KMS or Vault backend implements the same three. That is why
  the methods are `async` although this implementation never awaits: the choice of backend
  is a deployment decision and must not become a code change. Writing the second backend
  now, with no deployment asking for it, would be guessing at its shape.
- **No rotation CLI.** `rewrap_workspace` is a service method with a test and no caller.
  An operator command belongs with the deployment work in a later phase; what matters now
  is that the capability exists and is proven, so a KEK can be retired without a plan
  being invented under pressure.
- **No `use` caller.** Nothing calls it yet, because nothing makes an outbound call yet —
  `infra/net/guarded.py` is T2 and the provider is T3. It is written and tested now
  because the alternative is writing the decrypt path while also writing the first thing
  that needs it.
- **Nothing is refused while referenced.** The API table says `DELETE` is "refused while
  referenced". `model_profiles.credential_id` has `ON DELETE SET NULL` and this delete is
  a tombstone rather than a row removal, so there is no dangling reference to prevent —
  but a profile whose credential was destroyed is a profile that will fail at its next
  call. Refusing the delete needs the same "is this still in use" check that
  `ModelProfileService.delete` already has for agents, and it belongs with T3, where
  there is finally something that uses a credential to check against.

---

## 7. Open, and carried forward

Unchanged from `09-T0-PLAN.md` §open: **D7** (a cross-month retry double-counts
`runs.step_count` and `runs.tool_call_count`), **D11** (deleting an agent leaves its
island node), **D12** (`.shell__mark` contrast at 1.85:1).

New, from this task:

- **The development keyring is a fixed constant.** `local-dev-insecure`, derived from a
  fixed string, so a credential written before a restart is readable after it. Three
  things keep it out of a deployment: the deployed environment must set both variables,
  the check is on the key *material* rather than the id so pasting it in under a
  convincing name is refused, and every row records the `key_id` that wrapped it, so
  `SELECT count(*) FROM credentials WHERE key_id = 'local-dev-insecure'` answers "is any
  of this protected by the development key?" in one query.
- **Dedup of credential names spans tombstones.** `credentials_name_key` is
  `(workspace_id, provider_slug, name)` with no partial predicate, so a deleted
  credential's name stays taken. Deliberate for now — reusing the name of a destroyed
  credential is how an audit trail becomes ambiguous — but it means an operator who
  deletes and recreates has to pick a new name, and that will eventually be reported as a
  bug. Worth a decision, not worth a migration yet.
