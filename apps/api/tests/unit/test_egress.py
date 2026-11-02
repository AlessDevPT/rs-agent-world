"""The egress guard: every row of the blocked set, and the pinning.

Parametrised over `BLOCKED_IPV4` and `BLOCKED_IPV6` themselves rather than
over a sample, so "every range in section 3 is refused" is a statement about
the table that ships rather than about the dozen cases someone thought of.
Adding a row to the policy adds a test; deleting one deletes its test, which
is why `test_the_tables_are_the_documented_size` is here.

The pinning tests are the ones that matter most. A resolver that answers
differently the second time is the DNS-rebinding attack in one object, and a
resolver that counts its calls is how "there is no second resolution" is
asserted rather than assumed.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

import pytest

from agentworld.infra import net
from agentworld.infra.net import guarded

#: A policy that permits loopback, for the few tests that need a destination
#: they can actually connect to. Never the default - the test right below
#: asserts the default refuses it.
LOOPBACK_OK = net.EgressPolicy(allowed_networks=frozenset({ip_network("127.0.0.0/8")}))

PUBLIC = IPv4Address("93.184.216.34")
PUBLIC_SIX = IPv6Address("2606:2800:220:1:248:1893:25c8:1946")


class SpyResolver:
    """A resolver that records its calls and can change its mind.

    `answers` is consumed one call at a time; the last entry repeats. That
    is exactly what a rebinding attacker's DNS server does.
    """

    def __init__(self, *answers: Sequence[net.Address]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[net.Address]:
        self.calls.append((host, port))
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        return self.answers[index]


def _representative(network: IPv4Network | IPv6Network) -> IPv4Address | IPv6Address:
    """An address inside `network`, avoiding the network address for a /8-ish
    range where it may be special in its own right."""
    if network.num_addresses == 1:
        return network.network_address
    return network.network_address + 1


def _url(address: IPv4Address | IPv6Address, scheme: str = "http") -> str:
    host = str(address) if isinstance(address, IPv4Address) else f"[{address}]"
    return f"{scheme}://{host}/thing"


# ------------------------------------------------------------- the blocked set ---


@pytest.mark.parametrize(("cidr", "why"), net.BLOCKED_IPV4, ids=lambda value: str(value))
async def test_every_blocked_ipv4_range_is_refused(cidr: str, why: str) -> None:
    address = _representative(IPv4Network(cidr))
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate(_url(address), net.EgressPolicy())
    assert excinfo.value.failure is net.OutboundFailure.BLOCKED_DESTINATION
    # The log-only reason names a row, which is what an operator needs. Not
    # necessarily *this* row: the table overlaps on purpose, and
    # 255.255.255.255 is inside 240.0.0.0/4 as well as being its own /32.
    assert any(row in excinfo.value.reason for row, _ in net.BLOCKED_IPV4)


@pytest.mark.parametrize(("cidr", "why"), net.BLOCKED_IPV6, ids=lambda value: str(value))
async def test_every_blocked_ipv6_range_is_refused(cidr: str, why: str) -> None:
    address = _representative(IPv6Network(cidr))
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate(_url(address), net.EgressPolicy())
    assert excinfo.value.failure is net.OutboundFailure.BLOCKED_DESTINATION


def test_the_tables_are_the_documented_size() -> None:
    """Guard on the parametrisation.

    Both tests above pass vacuously if a table is emptied, and a table is
    exactly the kind of thing a careless merge truncates.
    """
    assert len(net.BLOCKED_IPV4) == 15
    assert len(net.BLOCKED_IPV6) == 11


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "::ffff:169.254.169.254", "fd00:ec2::254", "100.100.100.200"],
)
async def test_the_metadata_addresses_are_refused(address: str) -> None:
    """Each provider's metadata address, by the range that catches it.

    `fd00:ec2::254` is AWS's IPv6 metadata address, and it is caught by
    `fc00::/7` - not by `fe80::/10`, which is what
    `06-OUTBOUND-SECURITY.md` section 3 claims. `fd` is inside the
    unique-local prefix, not the link-local one. Both rows are in the table,
    so the document's mistake never became a hole, but it is a mistake.
    """
    with pytest.raises(net.OutboundError):
        await net.validate(_url(ip_address(address)), net.EgressPolicy())


# ------------------------------------------------------------- normalisation ---


@pytest.mark.parametrize(
    "written", ["10.0.0.1", "::ffff:10.0.0.1", "::ffff:a00:1", "64:ff9b::a00:1"]
)
def test_one_destination_however_it_is_written(written: str) -> None:
    """All four spellings normalise to the same address.

    Not "all four are refused", which would pass with four unrelated rules.
    """
    assert net.normalise(ip_address(written)) == IPv4Address("10.0.0.1")


@pytest.mark.parametrize("written", ["127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1"])
async def test_the_short_decimal_hex_and_octal_forms_of_loopback_are_refused(
    written: str,
) -> None:
    """`ipaddress` rejects all of these; the C resolver accepts them.

    So does `parse_literal`, which is the point: without it these fall
    through to "it must be a hostname" and the check depends on what the
    resolver does with them.

    `0177.0.0.1` goes one step further: httpx refuses to parse it at all, so
    the refusal has to come from the parse failure being normalised into a
    blocked destination rather than from the address check. Before that was
    handled, this case raised `httpx.InvalidURL` out of `validate` - a 500
    for a malformed string, and a 502 for an internal address, which is both
    a crash and an oracle.
    """
    if written != "0177.0.0.1":
        assert net.parse_literal(written) == IPv4Address("127.0.0.1")
    with pytest.raises(net.OutboundError):
        await net.validate(f"http://{written}/", net.EgressPolicy())


async def test_a_nat64_wrapped_public_address_is_still_refused() -> None:
    """Normalisation says 8.8.8.8; the range says no, and the range wins.

    A NAT64 prefix is a route into someone's network whatever is embedded
    in it, which is why `is_blocked` tests the literal as well as the
    normalised form.
    """
    assert net.normalise(ip_address("64:ff9b::8.8.8.8")) == IPv4Address("8.8.8.8")
    with pytest.raises(net.OutboundError):
        await net.validate("http://[64:ff9b::8.8.8.8]/", net.EgressPolicy())


def test_a_hostname_is_not_an_address() -> None:
    assert net.parse_literal("example.test") is None
    assert net.parse_literal("") is None


# ----------------------------------------------------------- the URL itself ---


@pytest.mark.parametrize("scheme", ["file", "gopher", "dict", "ftp", "redis", "jar"])
async def test_only_http_and_https_are_allowed(scheme: str) -> None:
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate(f"{scheme}://example.test/x", net.EgressPolicy())
    assert "not http or https" in excinfo.value.reason


async def test_a_url_carrying_credentials_is_refused() -> None:
    """Refused rather than stripped: they would be logged by anything that
    logs a URL, and this is not how a credential reaches an outbound call."""
    resolver = SpyResolver([PUBLIC])
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate("https://user:pass@example.test/x", net.EgressPolicy(), resolve=resolver)
    assert "userinfo" in excinfo.value.reason
    assert "pass" not in excinfo.value.reason
    assert resolver.calls == []


@pytest.mark.parametrize("port", [22, 6379, 3306, 8080])
async def test_a_port_outside_the_policy_is_refused(port: int) -> None:
    resolver = SpyResolver([PUBLIC])
    with pytest.raises(net.OutboundError):
        await net.validate(f"http://example.test:{port}/x", net.EgressPolicy(), resolve=resolver)
    assert resolver.calls == []


@pytest.mark.parametrize("port", [80, 443, 8443])
async def test_the_default_ports_are_allowed(port: int) -> None:
    resolver = SpyResolver([PUBLIC])
    scheme = "https" if port != 80 else "http"
    destination = await net.validate(
        f"{scheme}://example.test:{port}/x", net.EgressPolicy(), resolve=resolver
    )
    assert destination.address == PUBLIC


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal",
        "METADATA.GOOGLE.INTERNAL",
        "metadata.goog",
        "instance-data",
        "metadata.azure.com",
        "anything.metadata.google.internal",
        "metadata.google.internal.",
    ],
)
async def test_the_metadata_hostnames_are_refused_by_name(host: str) -> None:
    """Refused before resolution, with the resolver answering publicly.

    A provider may answer these through a mechanism that is not plain DNS,
    so blocking the address they usually give is not enough. The resolver
    returning a public address is what makes this test about the name.
    """
    resolver = SpyResolver([PUBLIC])
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate(f"http://{host}/x", net.EgressPolicy(), resolve=resolver)
    assert "metadata hostname" in excinfo.value.reason
    assert resolver.calls == [], "the name check must run before resolution"


# ------------------------------------------------------- resolution and pinning ---


async def test_a_mixed_answer_refuses_the_whole_request() -> None:
    """Not "pick the good one".

    A hostname that round-robins between a public and a private address
    would then work most of the time, which is worse than never working: it
    passes review and fails in production, or the reverse.
    """
    resolver = SpyResolver([PUBLIC, IPv4Address("10.0.0.5")])
    with pytest.raises(net.OutboundError) as excinfo:
        await net.validate("http://evil.test/x", net.EgressPolicy(), resolve=resolver)
    assert excinfo.value.failure is net.OutboundFailure.BLOCKED_DESTINATION
    assert "10.0.0.5" in excinfo.value.reason


async def test_several_public_answers_connect_to_one_of_them() -> None:
    resolver = SpyResolver([PUBLIC, PUBLIC_SIX])
    destination = await net.validate("http://example.test/x", net.EgressPolicy(), resolve=resolver)
    assert destination.address in (PUBLIC, PUBLIC_SIX)


async def test_the_address_is_pinned_against_a_rebinding_resolver() -> None:
    """The DNS-rebinding case, in one object.

    The resolver answers publicly the first time and privately after. A
    design that resolved again to connect would reach 10.0.0.5. This one
    has already decided, and the decision is an address.
    """
    resolver = SpyResolver([PUBLIC], [IPv4Address("10.0.0.5")])
    destination = await net.validate("http://evil.test/x", net.EgressPolicy(), resolve=resolver)
    assert destination.address == PUBLIC
    assert destination.url.host == str(PUBLIC)


async def test_exactly_one_lookup_happens_per_request() -> None:
    """There is no check-to-use window because there is no second check.

    Asserted by counting, because "we resolve once" is the kind of claim
    that stays true until someone adds a retry.
    """
    resolver = SpyResolver([PUBLIC])
    await net.validate("http://example.test/x", net.EgressPolicy(), resolve=resolver)
    assert resolver.calls == [("example.test", 80)]


async def test_a_host_that_resolves_to_nothing_is_refused() -> None:
    resolver = SpyResolver([])
    with pytest.raises(net.OutboundError):
        await net.validate("http://example.test/x", net.EgressPolicy(), resolve=resolver)


async def test_the_pinned_url_keeps_the_path_and_query() -> None:
    resolver = SpyResolver([PUBLIC])
    destination = await net.validate(
        "https://example.test/a/b?c=d", net.EgressPolicy(), resolve=resolver
    )
    assert destination.url.path == "/a/b"
    assert destination.url.query == b"c=d"
    assert destination.host_header == "example.test"
    assert destination.sni_hostname == "example.test"


async def test_a_non_default_port_is_carried_in_the_host_header() -> None:
    """`Host` includes a non-default port, and a virtual host may be
    configured on exactly that."""
    resolver = SpyResolver([PUBLIC])
    destination = await net.validate(
        "https://example.test:8443/x", net.EgressPolicy(), resolve=resolver
    )
    assert destination.host_header == "example.test:8443"


async def test_an_ipv6_answer_is_pinned_in_brackets() -> None:
    resolver = SpyResolver([PUBLIC_SIX])
    destination = await net.validate("http://example.test/x", net.EgressPolicy(), resolve=resolver)
    assert destination.url.host == str(PUBLIC_SIX)
    assert str(destination.url).startswith("http://[")


async def test_an_address_the_caller_wrote_gets_no_sni() -> None:
    """There is no name to present, and a certificate for an address is
    verified as an address."""
    destination = await net.validate(f"{_url(PUBLIC, 'https')}", net.EgressPolicy())
    assert destination.sni_hostname is None


# --------------------------------------------------------- the escape hatch ---


async def test_the_default_policy_blocks_loopback() -> None:
    """Guard on `allowed_networks`, the one field that can weaken the policy.

    Every test below that uses `LOOPBACK_OK` is only meaningful while this
    passes.
    """
    with pytest.raises(net.OutboundError):
        await net.validate("http://127.0.0.1/x", net.EgressPolicy())


async def test_a_deployment_may_allow_a_range_it_means_to_reach() -> None:
    destination = await net.validate("http://127.0.0.1/x", LOOPBACK_OK)
    assert destination.address == IPv4Address("127.0.0.1")


async def test_allowing_a_wrapper_does_not_allow_what_is_inside_it() -> None:
    """The escape hatch must not become a bypass.

    A deployment that genuinely sits behind NAT64 may allow
    `64:ff9b::/96`. That is a statement about a transition mechanism, not
    permission to reach `10.0.0.1` through it - and `64:ff9b::a00:1` is
    exactly that address with the prefix on.

    This is the case that makes normalisation load-bearing rather than
    decorative. With the default policy every alternative encoding is
    already inside a blocked range in its own right, so normalising changes
    no outcome; here the wrapper is permitted and only the normalised form
    refuses it.
    """
    nat64 = net.EgressPolicy(allowed_networks=frozenset({ip_network("64:ff9b::/96")}))
    with pytest.raises(net.OutboundError):
        await net.validate("http://[64:ff9b::a00:1]/x", nat64)
    # The same policy does permit a NAT64 address wrapping a public one.
    destination = await net.validate("http://[64:ff9b::8.8.8.8]/x", nat64)
    assert destination.address == ip_address("64:ff9b::8.8.8.8")


async def test_allowing_one_family_does_not_allow_the_other() -> None:
    with pytest.raises(net.OutboundError):
        await net.validate("http://[::1]/x", LOOPBACK_OK)


# ------------------------------------------------------- error normalisation ---


def test_a_blocked_destination_and_an_unreachable_one_read_the_same() -> None:
    """Section 5: an attacker gets the same answer from a blocked address as
    from one that does not exist."""
    blocked = net.OutboundError(net.OutboundFailure.BLOCKED_DESTINATION, reason="10.0.0.0/8")
    missing = net.OutboundError(net.OutboundFailure.UNREACHABLE, reason="NXDOMAIN")
    refused = net.OutboundError(net.OutboundFailure.UNREACHABLE, reason="ECONNREFUSED")

    details = {str(exc) for exc in (blocked, missing, refused)}
    assert len(details) == 1
    assert {exc.public_detail for exc in (blocked, missing, refused)} == details


def test_the_reason_stays_out_of_the_representation() -> None:
    """A `repr` reaches tracebacks and debugger frames written by code that
    never thought about this module."""
    exc = net.OutboundError(
        net.OutboundFailure.BLOCKED_DESTINATION, reason="10.0.0.5 is in 10.0.0.0/8"
    )
    assert "10.0.0.5" not in repr(exc)
    assert "10.0.0.5" not in str(exc)


def test_the_problem_document_is_identical_for_all_three() -> None:
    """The same sentence, the same code, the same status - one document."""
    from agentworld.infra.net.errors import as_domain_error

    documents = set()
    for failure, reason in (
        (net.OutboundFailure.BLOCKED_DESTINATION, "169.254.0.0/16"),
        (net.OutboundFailure.UNREACHABLE, "NXDOMAIN"),
        (net.OutboundFailure.UNREACHABLE, "ECONNREFUSED"),
    ):
        error = as_domain_error(net.OutboundError(failure, reason=reason))
        documents.add((error.code, error.title, error.detail))
    assert len(documents) == 1


def test_only_the_log_can_tell_them_apart() -> None:
    """Stated as a test so the asymmetry is deliberate rather than lucky."""
    blocked = net.OutboundError(net.OutboundFailure.BLOCKED_DESTINATION, reason="10.0.0.0/8")
    missing = net.OutboundError(net.OutboundFailure.UNREACHABLE, reason="NXDOMAIN")
    assert blocked.reason != missing.reason
    assert blocked.failure is not missing.failure


@pytest.mark.parametrize(
    ("failure", "retryable"),
    [
        (net.OutboundFailure.BLOCKED_DESTINATION, False),
        (net.OutboundFailure.UNREACHABLE, True),
        (net.OutboundFailure.TIMEOUT, True),
        (net.OutboundFailure.TLS_FAILURE, False),
        (net.OutboundFailure.HTTP_ERROR, False),
    ],
)
def test_what_a_retry_could_possibly_fix(failure: net.OutboundFailure, retryable: bool) -> None:
    assert failure.is_retryable is retryable


async def test_a_name_that_does_not_resolve_becomes_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`system_resolver` turns a DNS failure into the normalised kind.

    Monkeypatched rather than pointed at a `.invalid` name: a network whose
    resolver hijacks NXDOMAIN would otherwise fail this test for a reason
    that has nothing to do with the code.
    """

    async def refuse(*_: object, **__: object) -> None:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", refuse)
    with pytest.raises(net.OutboundError) as excinfo:
        await guarded.system_resolver("nowhere.test", 443)
    assert excinfo.value.failure is net.OutboundFailure.UNREACHABLE


async def test_a_blocked_destination_is_refused_with_no_socket_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constant-time rejection, asserted with a socket spy.

    A refusal that opened a connection first would take as long as the
    network does, and the difference between 1ms and 10s is information.
    """
    opened: list[object] = []
    real = socket.socket

    def spy(*args: object, **kwargs: object) -> object:
        opened.append(args)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "socket", spy)
    async with net.GuardedClient() as client:
        with pytest.raises(net.OutboundError):
            await client.request("GET", "http://169.254.169.254/latest/meta-data/")
    assert opened == []
