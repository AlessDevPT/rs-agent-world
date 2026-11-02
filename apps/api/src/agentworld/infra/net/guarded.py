"""The only HTTP client an outbound caller may construct.

```
1. Parse the URL. Refuse any scheme but http and https.
2. Refuse userinfo, a port outside the policy, and the metadata hostnames.
3. Resolve the host to every address it has.
4. Validate EVERY resolved address. One bad address refuses the whole
   request - not "pick a good one", because a round-robin answer would
   then pass intermittently and fail a security review by luck.
5. Connect to a validated ADDRESS, carrying the original Host header and
   TLS SNI so virtual hosting and certificate verification still work.
6. Never resolve again for this request.
```

Step 5 is the whole design. The connection is made to an address that has
already been checked, so there is no second resolution to poison: **DNS
rebinding and the check-to-use window are closed by construction**, not by
shortening a race. `validate` is also the function a save-time check calls,
so "validated at save time and at request time" is one function rather than
two that can drift.

The pinning is done by rewriting the URL's host to the address and setting
`Host` and the `sni_hostname` extension, rather than by subclassing a
transport and overriding resolution. Both work; this one is built out of
httpx's public API, so it cannot be broken by a change inside httpcore, and
it is short enough to read in one sitting - which for the one module that
every outbound request passes through is worth more than elegance.

**Redirects are never followed.** `06-OUTBOUND-SECURITY.md` section 4
describes an opt-in mode where each hop is independently resolved, validated
and pinned. It is not built here, because nothing opts in yet and a redirect
follower with no caller is a security-critical code path with no test that
reflects how it will be used. The client returns the 3xx and its `Location`
to the caller; the first caller that wants to follow one brings its
requirements with it.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address

import httpx

from agentworld.infra.logging import get_logger
from agentworld.infra.net.addresses import Address, blocking_rule, is_blocked, parse_literal
from agentworld.infra.net.errors import OutboundError, OutboundFailure
from agentworld.infra.net.policy import ALLOWED_SCHEMES, BLOCKED_HOSTNAMES, EgressPolicy

#: Given a host and a port, every address that host has. Injectable because
#: the pinning guarantee is only testable with a resolver that lies - and
#: because the acceptance criteria ask for a spy that counts the lookups.
Resolver = Callable[[str, int], Awaitable[Sequence[Address]]]

_DEFAULT_PORTS = {"http": 80, "https": 443}

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Destination:
    """A validated request target: where to connect, and who to say it is."""

    #: The address the connection is pinned to. Already validated.
    address: Address
    #: The URL with its host replaced by `address`.
    url: httpx.URL
    #: The original authority, for the `Host` header - with the port when it
    #: is not the scheme's default, because `Host` includes a non-default
    #: port and a virtual host may be configured on exactly that.
    host_header: str
    #: The original hostname, for TLS. None for plain http, and None when the
    #: caller asked for an address in the first place: there is no name to
    #: present and a certificate for an IP is verified as an IP.
    sni_hostname: str | None


@dataclass(frozen=True, slots=True)
class GuardedResponse:
    """What came back, with the body bounded.

    Headers and body are here because a caller that cannot see them cannot
    do anything with the response. Keeping them out of step output, event
    payloads and model context is the caller's obligation and is asserted
    where that happens; this type is the inside of the boundary.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    #: True when the body hit `max_response_bytes` and was cut. The caller
    #: reports this rather than silently handing on a half-document.
    truncated: bool


async def system_resolver(host: str, port: int) -> Sequence[Address]:
    """Every A and AAAA answer for `host`, through the event loop's resolver."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundError(
            OutboundFailure.UNREACHABLE, reason=f"getaddrinfo failed: {exc.strerror}"
        ) from None
    return [ip_address(info[4][0]) for info in infos]


async def validate(
    url: str | httpx.URL,
    policy: EgressPolicy,
    *,
    resolve: Resolver = system_resolver,
) -> Destination:
    """Check a URL and decide the one address a request to it may use.

    Raises `OutboundError(BLOCKED_DESTINATION)` before any connection is
    attempted, which is what makes a refusal cost the same whether the
    destination is an internal host or a reserved range - there is no
    connection whose timing could differ.

    The same function backs save-time validation, where the point is to tell
    a user straight away that their URL will not work, and request-time
    validation, where the point is security. A record that resolves publicly
    today and privately tomorrow is caught by the second, which is the one
    that counts.
    """
    try:
        parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except httpx.InvalidURL as exc:
        # A URL httpx will not parse is a destination this cannot reach, and
        # it arrives here from a model or a user. Letting `InvalidURL` escape
        # would make a malformed string a 500 and an internal address a 502,
        # which is both a crash and an oracle. `0177.0.0.1` is the case that
        # found this: a loopback address in octal, which httpx rejects as a
        # malformed IPv4 literal before any check here runs.
        _refuse(f"the URL could not be parsed: {type(exc).__name__}")

    if parsed.scheme not in ALLOWED_SCHEMES:
        _refuse(f"scheme '{parsed.scheme}' is not http or https")
    if parsed.userinfo:
        # Refused rather than stripped: credentials in a URL would be logged
        # by anything that logs a URL, and this is not how a credential
        # reaches an outbound call here.
        _refuse("the URL carries userinfo")

    host = parsed.host
    if not host:
        _refuse("the URL has no host")
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    if not policy.permits_port(port):
        _refuse(f"port {port} is outside the policy")
    if _is_blocked_name(host):
        # Before resolution on purpose: a provider may answer these through
        # something that is not plain DNS.
        _refuse(f"'{host}' is a cloud metadata hostname")

    literal = parse_literal(host)
    if literal is not None:
        if is_blocked(literal, policy):
            _refuse(f"{literal} is in {blocking_rule(literal)}")
        return _destination(parsed, literal, host, port, sni=None)

    addresses = await resolve(host, port)
    if not addresses:
        _refuse(f"'{host}' resolved to no addresses")
    for address in addresses:
        if is_blocked(address, policy):
            # One bad answer refuses the whole request. Choosing a good one
            # from a mixed answer means a hostname that round-robins between
            # a public and a private address works most of the time, which
            # is worse than never working.
            _refuse(f"'{host}' resolves to {address}, in {blocking_rule(address)}")

    return _destination(parsed, addresses[0], host, port, sni=host)


class GuardedClient:
    """An httpx client that can only reach validated, pinned addresses.

    One instance per caller is fine and one per process is better: it holds
    a connection pool. `aclose` releases it.
    """

    def __init__(
        self,
        policy: EgressPolicy | None = None,
        *,
        resolve: Resolver = system_resolver,
        verify: ssl.SSLContext | str | bool = True,
    ) -> None:
        """`verify` is operator and test configuration, never user input.

        A user-supplied CA bundle is a user-supplied trust decision, and
        there is no path from a request body to this argument. It is here so
        a test can trust its own throwaway certificate and so a deployment
        behind a corporate CA can work at all.
        """
        self._policy = policy or EgressPolicy()
        self._resolve = resolve
        self._client = httpx.AsyncClient(
            # Never. Section 4: a redirect chain is not a trusted channel,
            # and following one means posting the work somewhere the user
            # never named.
            follow_redirects=False,
            verify=verify,
            timeout=httpx.Timeout(
                self._policy.total_timeout_seconds,
                connect=self._policy.connect_timeout_seconds,
            ),
        )

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> GuardedResponse:
        """Validate, pin, send, and bound the response body."""
        destination = await validate(url, self._policy, resolve=self._resolve)
        sent = dict(headers or {})
        # Last, so a caller cannot override the one header that makes a
        # pinned connection reach the right virtual host.
        sent["Host"] = destination.host_header

        extensions: dict[str, str] = {}
        if destination.sni_hostname is not None:
            # Certificate verification uses this name, not the address in
            # the URL, so a pinned connection still proves it reached the
            # host the caller asked for.
            extensions["sni_hostname"] = destination.sni_hostname

        try:
            async with self._client.stream(
                method,
                destination.url,
                headers=sent,
                content=content,
                extensions=extensions,
            ) as response:
                body, truncated = await self._read(response)
                return GuardedResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    content=body,
                    truncated=truncated,
                )
        except httpx.HTTPError as exc:
            raise _translate(exc) from None

    async def _read(self, response: httpx.Response) -> tuple[bytes, bool]:
        """Read at most `max_response_bytes`, then stop reading.

        Streamed rather than measured afterwards: `Content-Length` is the
        sender's claim, and a sender that lies about it is exactly the case
        the cap exists for.
        """
        limit = self._policy.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            remaining = limit - total
            if len(chunk) >= remaining:
                chunks.append(chunk[:remaining])
                return b"".join(chunks), True
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GuardedClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def guarded_client(policy: EgressPolicy | None = None, **kwargs: object) -> GuardedClient:
    """The name `06-OUTBOUND-SECURITY.md` section 2 uses."""
    return GuardedClient(policy, **kwargs)  # type: ignore[arg-type]


def _destination(
    url: httpx.URL, address: Address, host: str, port: int, *, sni: str | None
) -> Destination:
    authority = host if port == _DEFAULT_PORTS[url.scheme] else f"{host}:{port}"
    pinned = str(address) if isinstance(address, IPv4Address) else f"[{address}]"
    return Destination(
        address=address,
        url=url.copy_with(host=pinned),
        host_header=authority,
        sni_hostname=sni,
    )


def _is_blocked_name(host: str) -> bool:
    name = host.rstrip(".").lower()
    return any(name == blocked or name.endswith(f".{blocked}") for blocked in BLOCKED_HOSTNAMES)


def _refuse(reason: str) -> None:
    """Refuse a destination, loudly in the log and silently to the caller."""
    log.warning("outbound_destination_blocked", reason=reason)
    raise OutboundError(OutboundFailure.BLOCKED_DESTINATION, reason=reason)


def _translate(exc: httpx.HTTPError) -> OutboundError:
    """One httpx exception tree, five failure kinds, no upstream text.

    The TLS check walks the cause chain rather than matching on the
    exception type: httpx reports a certificate failure as a `ConnectError`
    whose `__cause__` is an `ssl.SSLError`, so the type alone cannot tell a
    name mismatch from a refused connection.
    """
    if _caused_by_tls(exc):
        return OutboundError(OutboundFailure.TLS_FAILURE, reason=type(exc).__name__)
    if isinstance(exc, httpx.TimeoutException):
        return OutboundError(OutboundFailure.TIMEOUT, reason=type(exc).__name__)
    return OutboundError(OutboundFailure.UNREACHABLE, reason=type(exc).__name__)


def _caused_by_tls(exc: BaseException) -> bool:
    """Walk `__cause__` *and* `__context__` looking for an SSL error.

    Both, because the chain uses both: httpx re-raises httpcore's exception
    with `from exc`, which sets `__cause__`, but httpcore's own handler
    catches the `ssl.SSLCertVerificationError` and raises without `from`, so
    that link is only in `__context__`. Following `__cause__` alone finds
    nothing and reports a certificate failure as `unreachable` - which is
    what the two TLS tests caught.

    Depth-bounded rather than loop-detecting: three links is already more
    than this chain has, and an exception graph with a cycle in it is not
    worth a visited set here.
    """
    seen: BaseException | None = exc
    for _ in range(6):
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False
