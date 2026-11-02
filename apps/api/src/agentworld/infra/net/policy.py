"""What an outbound request may reach, as data.

The tables here are transcribed from `06-OUTBOUND-SECURITY.md` section 3, one
entry per row of that document, so the two can be compared by eye. The tests
are parametrised over these tuples rather than over a sample, which is what
makes "every range in section 3 is refused" a statement about the whole table.

`EgressPolicy` is a frozen dataclass and every field has the default the
document specifies, so `EgressPolicy()` *is* the documented policy. A
deployment widens it deliberately and visibly - a self-hoster whose workers
should reach a LAN service, a deployment that runs something on port 9000 -
and the widening is then one value in one object rather than a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network
from typing import Final

#: Blocked IPv4 ranges. One entry per row of section 3's IPv4 table, with the
#: row's own description, because an unexplained CIDR in a security control is
#: a CIDR nobody will ever dare change.
BLOCKED_IPV4: Final[tuple[tuple[str, str], ...]] = (
    ("0.0.0.0/8", "this host"),
    ("10.0.0.0/8", "private (RFC 1918)"),
    ("172.16.0.0/12", "private (RFC 1918)"),
    ("192.168.0.0/16", "private (RFC 1918)"),
    # The whole /8. A check for 127.0.0.1 alone misses 127.0.0.2, which is
    # the same interface.
    ("127.0.0.0/8", "loopback"),
    # Contains 169.254.169.254, the cloud metadata address.
    ("169.254.0.0/16", "link-local"),
    ("100.64.0.0/10", "carrier-grade NAT (RFC 6598)"),
    ("192.0.0.0/24", "IETF protocol assignments"),
    ("192.0.2.0/24", "documentation (TEST-NET-1)"),
    ("198.51.100.0/24", "documentation (TEST-NET-2)"),
    ("203.0.113.0/24", "documentation (TEST-NET-3)"),
    ("198.18.0.0/15", "benchmarking"),
    ("224.0.0.0/4", "multicast"),
    ("240.0.0.0/4", "reserved"),
    ("255.255.255.255/32", "broadcast"),
)

#: Blocked IPv6 ranges, likewise one per row of section 3's IPv6 table.
BLOCKED_IPV6: Final[tuple[tuple[str, str], ...]] = (
    ("::/128", "unspecified"),
    ("::1/128", "loopback"),
    # fe80::/10 covers fd00:ec2::254 on AWS... it does not, and that is worth
    # saying: fd00:ec2::254 is inside fc00::/7, the row below. Both are here.
    ("fe80::/10", "link-local"),
    ("fc00::/7", "unique local"),
    # The most common bypass. Blocked as a whole range rather than by
    # re-applying the IPv4 rules to the embedded address: nothing legitimate
    # resolves to a v4-mapped address through getaddrinfo, which returns
    # plain AF_INET results for IPv4, so the whole /96 can go.
    ("::ffff:0:0/96", "IPv4-mapped"),
    ("64:ff9b::/96", "NAT64"),
    ("64:ff9b:1::/48", "NAT64 (local-use prefix)"),
    ("2002::/16", "6to4"),
    ("2001::/32", "Teredo"),
    ("100::/64", "discard-only"),
    ("ff00::/8", "multicast"),
)

#: Hostnames refused before resolution. A provider may resolve these through
#: a mechanism that is not plain DNS, so blocking the addresses they usually
#: answer with is not enough. Matched case-insensitively, on the exact name
#: and on any subdomain of it.
BLOCKED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata.azure.com",
    }
)

#: An allow-list of two, not a deny-list of the rest. `file://`, `gopher://`,
#: `dict://`, `ftp://` and `redis://` are the classic protocol-smuggling
#: vectors, and the next one invented is covered by not being on this list.
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Arbitrary ports are how internal service discovery is done. 8443 is here
#: because it is a common second HTTPS port on legitimate public services.
DEFAULT_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443, 8443})

_TEN_MB: Final = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """The egress rules for one caller. The defaults are the documented ones.

    `allowed_networks` is the deployment escape hatch, and the only field that
    can make the policy *weaker*. It exists because the alternative is worse:
    a self-hoster who needs to reach a service on their LAN, told that the
    application forbids it with no way to say otherwise, will reach for a
    patch or an unguarded client. An explicit allow-list is reviewable; a
    forked HTTP client is not.

    It is empty by default, and `test_the_default_policy_blocks_loopback`
    exists so that stays true.
    """

    #: Ports a request may use. Widening this is a deployment decision.
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS

    #: Networks that override the blocked tables. Empty by default.
    allowed_networks: frozenset[IPv4Network | IPv6Network] = field(default_factory=frozenset)

    #: A hung connect holds a dispatch lease, so this is deliberately short.
    connect_timeout_seconds: float = 5.0

    #: Bounds one step. The document's 30s for tools; `agent_push` passes 10.
    total_timeout_seconds: float = 30.0

    #: Read at most this much and report truncation. A tool returning 2 GB
    #: into a model's context is an availability and a cost problem.
    max_response_bytes: int = _TEN_MB

    def permits_port(self, port: int) -> bool:
        return port in self.allowed_ports
