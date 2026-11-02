# T3 — Provider interface and the first implementation

**Status: implemented, with one acceptance criterion unmet.** The live call against the
real API has **not** been made — see §5. Everything else is done and tested.

---

## 1. What was built

| | |
|---|---|
| `infra/providers/base.py` | `ModelProvider`, one method, and the neutral vocabulary it speaks |
| `infra/providers/anthropic.py` | the Messages API over the guarded client, no SDK |
| `infra/providers/registry.py` | slug → implementation, and nothing else |
| `scripts/live_provider_smoke.py` | the manual live check, deliberately not a test |
| `pyproject.toml` | an eighth contract: provider SDKs stay inside `infra/providers` |
| `tests/unit/test_vendor_containment.py` | the check no import contract can express |

56 tests. Nothing above `infra/providers/` names a vendor, and that is now asserted three
ways: by the import contract, by a source search, and by the fact that `registry.build`
takes a slug that arrived as data from a `model_profiles` row.

---

## 2. No vendor SDK

The Anthropic provider speaks the Messages API directly — one `POST` with a JSON body —
rather than through the `anthropic` package.

The reason is the acceptance criterion above it: *every provider call goes through the
guarded client*. An SDK brings its own HTTP client, with its own connection handling, its
own redirect policy and its own name resolution — none of which is the pinned,
address-validated one T2 built. Making the SDK borrow our transport means depending on an
internal of theirs that can change in a patch release, and the failure mode if it ever did
is silent: calls would still succeed, just not through the guard.

**The cost is real and worth stating.** New API features arrive when someone adds them here
rather than with a dependency bump, and the response parser is ours to keep correct. For a
surface this narrow — one endpoint, one response shape, about eighty lines — that is a
trade worth making. It would not be for a provider with a large API, and the next
implementation is free to make the opposite choice: the Protocol does not care.

The import contract still forbids the SDK outside this package, even though nothing
installs it. import-linter reads source rather than importing, so a planted `import
anthropic` in `services/` is caught statically — which is exactly what the non-vacuity pass
confirmed.

---

## 3. What the interface carries, and what it does not

**Tool definitions go out; tool calls come back; nothing here executes one.** A model
answering with a tool call is the model saying what it would like done. Turning that into an
action is the step runner's decision, gated by the grant that allowed the tool and — once
Phase 3a adds them — by an approval. This layer could not act on a tool call if it wanted
to: it has no tool registry, no dispatcher and no network beyond the one call it just made.

They are in the interface rather than deferred because text-in-text-out is portable between
any two providers *by accident*. Tool calling is where provider APIs actually differ, so an
interface that has never carried a tool definition has not been tested as an interface —
and T13's whole purpose is to find out whether this one is shaped around its first
implementation.

**Multi-part content is not modelled.** `Message.content` is text. Images, documents and
citations are each a different shape, and modelling three of them before a caller needs one
means taking the abstraction from a guess.

**Stop reasons are mapped, never passed through.** A vendor string reaching a caller that
switches on it is how the next provider ends up having to emit this one's vocabulary.
`StopReason.OTHER` is the escape hatch, and an unknown reason lands there rather than
raising — a provider adding a stop reason should not break a deployment.

**An unknown content block is dropped with a log line**, for the same reason. The text and
tool calls that *were* understood are still a usable answer.

**Missing token counts are an error, not a zero.** A completion that silently reports
costing nothing is worse than one that fails: the first is a spend report that is quietly
wrong, and it is wrong in the direction nobody investigates.

---

## 4. Classification

| Status | Failure | Retryable |
|---|---|---|
| 400, 404, 413, 422 | `INVALID_REQUEST` | no |
| 401, 403 | `UNAUTHORIZED` | no |
| 429 | `RATE_LIMITED` | yes, honouring `Retry-After` |
| 529 | `OVERLOADED` | yes |
| other 5xx | `UNAVAILABLE` | yes |
| a 200 this parser cannot read | `MALFORMED_RESPONSE` | no |
| a 3xx (the guarded client does not follow it) | `MALFORMED_RESPONSE` | no |

Two decisions inside that table are worth their own sentence.

**`Retry-After` is honoured only in the delta-seconds form.** The HTTP-date form is legal
and this provider does not send it. Parsing a date into a wait means trusting this host's
clock against the provider's, which is how a skewed machine waits for an hour or does not
wait at all. An unparseable header yields `None`, which means "use your own backoff" — a
safe answer rather than a wrong one.

**A blocked destination becomes `INVALID_REQUEST`, not `UNAVAILABLE`.** A provider base URL
the egress policy refuses is a configuration mistake, and classifying it as retryable would
spend three attempts discovering that a URL is still refused. The error still says nothing
about *which* range refused it: T2's normalisation holds, and
`test_a_provider_cannot_reach_a_blocked_base_url` asserts the string contains no `169.254`.

---

## 5. Acceptance criteria

| Criterion | Test | |
|---|---|---|
| A one-message completion **against the real API** returns content and token counts | `scripts/live_provider_smoke.py` | ⬜ **not run** |
| A one-message completion returns content and token counts (deterministic) | `test_a_one_message_completion_returns_content_and_token_counts` | ✅ |
| Every provider call goes through the guarded client | the contract, plus `test_a_provider_cannot_reach_a_blocked_base_url` | ✅ |
| A 429 with `Retry-After` is classified retryable and honours the header | `test_a_rate_limit_honours_retry_after` | ✅ |
| 401 and 400 are classified terminal | `test_each_status_is_classified` | ✅ |
| Planting `import anthropic` in `services/` fails the import contract | verified by planting one | ✅ |
| Nothing outside `infra/providers/` names a vendor | `test_no_vendor_is_named_outside_the_provider_package`, verified by planting one | ✅ |

### The one that is not met

**No live call has been made.** It needs an `ANTHROPIC_API_KEY`, which this session does not
have and should not be given one. The script exists, is linted and type-checked, and exits
2 with a clear message when the key is absent — which is all that can be demonstrated
without the key.

```bash
ANTHROPIC_API_KEY=sk-ant-... apps/api/.venv/bin/python apps/api/scripts/live_provider_smoke.py
```

It is a **script, not a test, and deliberately not under `tests/`**. Q3 asks for
deterministic responses in ordinary CI with no key required, plus a manually initiated live
check kept separate. A pytest file guarded by `skipif` would satisfy the first two and break
a third thing: CI asserts the suite reports *zero* skips, and a conditionally-skipped live
test is a skip. As a script it cannot be collected, cannot be skipped, and cannot quietly
stop being run — because nothing was ever running it.

What it checks is the half a local server cannot: that the real endpoint resolves to an
address the default egress policy allows, that its certificate verifies through a pinned
connection with the original SNI, that the request shape is one the real API accepts, and
that the response shape is one this parser reads. It runs against the **default**
`EgressPolicy` on purpose — if the real endpoint does not pass the same rules a tool call
would, that is the finding.

### Deterministic, but not mocked

The automated tests go through the real `GuardedClient` to a real socket on loopback, with a
server that speaks the Messages protocol and records what it was sent. A mocked transport
would have been less code and would have tested less: it would happily deliver a request the
egress policy would have refused, and the seam between the provider and the guard is half of
what is worth testing here.

---

## 6. Non-vacuity

Nine guards, each weakened until a named test failed.

| Weakened | What failed |
|---|---|
| `Retry-After` never parsed | `test_a_rate_limit_honours_retry_after` |
| 401 classified as `INVALID_REQUEST` | `test_each_status_is_classified[401]` |
| `is_retryable` returning True for everything | `test_a_body_this_cannot_read_is_malformed_and_terminal` (all 6) + 5 more |
| missing `usage` tolerated as zero | `test_a_body_this_cannot_read_is_malformed_and_terminal[usage is missing]` |
| unknown stop reason defaulting to `COMPLETED` | `test_every_stop_reason_is_mapped_not_passed_through` (2 cases) |
| idempotency header not sent | `test_an_idempotency_token_is_forwarded_when_there_is_one` |
| blocked destination classified `UNAVAILABLE` | `test_a_provider_cannot_reach_a_blocked_base_url` |
| `import anthropic` planted in `services/projects.py` | the SDK contract broke |
| a vendor name and a model id planted in `services/projects.py` | the search test failed, naming both lines |

---

## 7. Open

- **The live call.** Named above, and the only unmet criterion in this task.
- **`google.generativeai` cannot be named in the import contract.** import-linter refuses a
  subpackage of an external package, and forbidding `google` wholesale would be absurd. The
  Gemini SDK is covered by the vendor-name search test instead. The gap is stated in
  `pyproject.toml` beside the contract rather than left to be discovered.
- **Streaming is not implemented.** Nothing needs it until a UI shows tokens as they
  arrive, and a streaming surface on the Protocol with no consumer is a second code path
  that the first provider would define the shape of.
- Carried forward: **D7**, **D11**, **D12**, T1's tombstone name collision, and T2's retry
  asymmetry between a blocked destination and an unreachable one.
