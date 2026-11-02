# T2 — The guarded HTTP client

**Status: implemented.** Phase 3, task T2. Lands before any caller exists, which is the
point: retrofitting one outbound client after four callers have each built their own means
auditing four.

`06-OUTBOUND-SECURITY.md` is the boundary document and this is its implementation. The
short version of what it buys: a request is validated against **every** address its host
resolves to, and the connection is then made to one of those addresses rather than to the
name. There is no second resolution, so DNS rebinding and the check-to-use window are
closed by construction rather than by shortening a race.

---

## 1. What was built

| | |
|---|---|
| `infra/net/policy.py` | the blocked tables as data, one entry per row of §3, and `EgressPolicy` |
| `infra/net/addresses.py` | parse, normalise, judge — the three spellings and the two wrappers |
| `infra/net/errors.py` | five failure kinds, and what a caller is allowed to be told |
| `infra/net/guarded.py` | validate, pin, send, bound the body |
| `infra/auth/__init__.py` | **new, and not cosmetic** — see §3 |
| `domain/errors.py` | `OutboundRequestFailedError`, a 502 |
| `pyproject.toml` | a seventh import contract: `httpx` only in `infra/net`, plus the one named exemption |

Two things are deliberately *not* here, and §6 says why: redirect following, and the
per-workspace concurrency limit.

---

## 2. Three corrections to the security document

### 2.1 `fd00:ec2::254` is not in `fe80::/10`

§3's IPv6 table reads "`fe80::/10` link-local, including **`fd00:ec2::254`** on AWS". It is
not: `fd` is `11111101`, whose first seven bits put it inside `fc00::/7`, the unique-local
row directly below. Both rows are in the table, so the document's mistake never became a
hole — AWS's IPv6 metadata address is blocked, by the row underneath the one that claims
it. `test_the_metadata_addresses_are_refused` covers it, with the correction in its
docstring so the next reader of §3 is not misled by it twice.

### 2.2 `blocked_destination` cannot be both a failure kind and invisible

§5 asks for a fixed set of five kinds *including* `blocked_destination`, and in the same
section asks that "an attacker gets the same answer from a blocked address as from one
that does not exist". If the kind is what the caller sees, those two cannot both hold.

So the kind is internal: it goes to the log and it decides whether a retry could help.
What leaves the module towards a caller is `public_detail`, and `BLOCKED_DESTINATION` and
`UNREACHABLE` share one sentence. `TIMEOUT`, `TLS_FAILURE` and `HTTP_ERROR` keep their own
wording and that is not an inconsistency: a timeout is self-evident from how long the
caller waited, a TLS failure can only happen after a connection was established, and an
HTTP error means the request arrived. None of them reveals anything the caller did not
already have.

**One consequence is not resolved here.** `BLOCKED_DESTINATION` is terminal and
`UNREACHABLE` is retryable, so the two are indistinguishable in their message and
distinguishable in how many attempts they consume. That is a decision for the retry policy
in T8 — either blocked destinations burn the same attempts, or per-attempt history is not
visible to the tenant. It is written down in `OutboundFailure.is_retryable` rather than
left for someone to notice.

### 2.3 A URL the client cannot parse is a refusal, not a crash

`http://0177.0.0.1/` — loopback in octal — is rejected by httpx's own URL parser before any
check in this module runs. The first version let `httpx.InvalidURL` escape from `validate`,
which would have made a malformed string a 500 and an internal address a 502: both a crash
and an oracle. A parse failure is now a `BLOCKED_DESTINATION` like any other.

---

## 3. A blind spot found while writing the contract

`src/agentworld/infra/auth/` had no `__init__.py`.

It worked at runtime, because Python treats it as an implicit namespace package. Two things
did not work, and neither announced itself:

**import-linter could not see it.** `grimp` walks packages and does not enter a namespace
package, so every architecture contract in `pyproject.toml` has been silently skipping the
module that verifies every user's JWT — since Phase 1, passing, and not looking. Adding the
file took the analysed-file count from 108 to 122. All contracts still pass, so nothing was
hiding in there; what was hiding was the fact that nothing was being checked.

**A non-editable install would have omitted it.** `[tool.setuptools.packages.find]` does not
pick up namespace packages, so a built wheel would have been missing
`agentworld.infra.auth` entirely and would have failed at the first import. CI installs
with `pip install -e`, which adds a path hook, so CI could never have caught it.

This is the same shape as the Phase 2 finding about `python -m importlinter.cli` exiting 0
without evaluating anything: a check that reports success while looking at nothing.

---

## 4. A bypass found by the non-vacuity pass

`is_blocked` consulted `allowed_networks` **first** and returned on a match. That reads
correctly — the deployment said it means to reach this range — and it is wrong.

A deployment that genuinely sits behind NAT64 may allow `64:ff9b::/96`. That is a statement
about a transition mechanism, not permission to reach `10.0.0.1` through it. With the
allow-list checked first, `64:ff9b::a00:1` matched the permitted wrapper and was accepted,
and the private address inside it was never examined.

The rule now is: refuse if **any** form of the address is blocked and is not itself
allowed. Allowing the wrapper does not allow what is inside it.

This was not found by thinking about NAT64. It was found by weakening normalisation and
discovering that **no test failed** — because with the default policy every alternative
encoding is already inside a blocked range in its own right, so normalising changed no
outcome anywhere. Looking for a case where it would matter is what turned up the hatch.

---

## 5. Acceptance criteria

From `08-PHASE-3-READINESS.md` §T2.

| Criterion | Test | |
|---|---|---|
| Every range in §3 refused, parametrised one case per row, both families | `test_every_blocked_ipv4_range_is_refused` (15), `test_every_blocked_ipv6_range_is_refused` (11) | ✅ |
| `::ffff:169.254.169.254` and the NAT64 form | `test_the_metadata_addresses_are_refused`, `test_a_nat64_wrapped_public_address_is_still_refused` | ✅ |
| A resolver answering public then `10.0.0.5` still connects to the first | `test_the_address_is_pinned_against_a_rebinding_resolver` | ✅ |
| `Host` and TLS SNI carry the original hostname, against a real TLS server | `test_the_host_header_and_sni_carry_the_original_hostname` | ✅ |
| Blocked, nonexistent and refused are indistinguishable | `test_a_blocked_destination_and_an_unreachable_one_read_the_same`, `test_the_problem_document_is_identical_for_all_three`, `test_a_refused_connection_is_unreachable` | ✅ |
| `httpx.AsyncClient` outside `infra/net/` fails the import contract | verified by planting one — see §6 | ✅ |

Also covered, from `06-OUTBOUND-SECURITY.md` §9: the short, decimal, hex and octal forms of
loopback; mixed DNS answers refused outright; several public answers connecting to one;
the four smuggling schemes; userinfo; ports 22 and 6379; each metadata hostname refused by
name with resolution stubbed to a public address; exactly one lookup per request; a blocked
destination refused with no socket opened; a body truncated at the limit; a `302` to the
metadata address returned rather than followed.

The TLS test deserves its own note. The criterion insists on a real server, and the
insistence is right: pinning rewrites the URL's host to an IP, and only a real handshake can
tell you whether the certificate was then verified against the name the caller asked for or
against the address the client happened to dial. A test server presenting a certificate for
`other.test` while the client asks for `example.test` is what proves the check is a check —
if SNI were dropped, or verification skipped because the host is an IP, that case would
succeed.

---

## 6. Non-vacuity

Ten guards weakened, each until a named test failed.

| Weakened | What failed |
|---|---|
| `inet_aton` fallback removed | `test_the_short_decimal_hex_and_octal_forms_of_loopback_are_refused` (3 forms) |
| "one bad address refuses the request" → pick a good one | `test_a_mixed_answer_refuses_the_whole_request` |
| metadata-name check moved after resolution | `test_the_metadata_hostnames_are_refused_by_name` (all 7) |
| `sni_hostname` extension not sent | `test_the_host_header_and_sni_carry_the_original_hostname` |
| `Host` header not set | `test_the_host_header_and_sni_carry_the_original_hostname` |
| blocked and unreachable given different sentences | `test_a_blocked_destination_and_an_unreachable_one_read_the_same`, `test_the_problem_document_is_identical_for_all_three` |
| body cap removed | `test_a_body_over_the_limit_is_truncated` |
| `follow_redirects=True` | `test_a_redirect_is_returned_and_not_followed` |
| normalisation removed from `is_blocked` | `test_allowing_a_wrapper_does_not_allow_what_is_inside_it` — **after** §4; before it, nothing failed |
| `import httpx` planted in `services/projects.py` | the contract broke, naming the file and line |

The last one is the structural criterion and the only one that plants a violation rather
than removing a guard, because an import contract has no other way to be shown to bite.

---

## 7. What T2 does not do

- **Redirects are refused, not followed.** §4 describes an opt-in mode where each hop is
  independently resolved, validated and pinned. It is not built, because nothing opts in
  yet and a redirect follower with no caller is a security-critical path with no test that
  reflects how it will be used. The client returns the `3xx` and its `Location`; the first
  caller that wants hops brings its requirements with it.
- **No per-workspace concurrency limit.** §6 of the security document asks for 20
  concurrent outbound requests per workspace. There is no workspace in scope here — the
  client takes a policy, not a tenant — and the limit belongs with the dispatch worker that
  will own the queue. T6 or T8.
- **No save-time validation caller.** `validate` is the single function the document's
  "validated at save time *and* at request time" requires, and `request` calls it. The
  save-time caller arrives with the entity that has a URL to save.
- **It does not stop an agent reaching a genuinely public endpoint it should not.** §8 of
  the security document is the honest list and it has not changed: the controls for that
  are the tool grant, the `consequential` classification and the approval gate.
- **It is defence in depth, not the boundary.** §5's table is the boundary: workers that
  cannot route to the internal network at all. A self-hoster on bare metal is relying
  entirely on this module, and the document says so.

---

## 8. Open

- The retry asymmetry in §2.2, for T8.
- `EgressPolicy.allowed_networks` is the one field that can weaken the policy.
  `test_the_default_policy_blocks_loopback` is the guard on it, and §4's rule means
  allowing a range no longer allows what can be wrapped inside it. It is still a field a
  deployment can get wrong, and it is the field a self-hoster is most likely to touch.
- Carried forward from earlier tasks: **D7** (cross-month dedup double-counts two
  counters), **D11** (deleting an agent leaves its island node), **D12**
  (`.shell__mark` contrast), and T1's tombstone name collision.
