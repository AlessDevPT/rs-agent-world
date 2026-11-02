"""Turning whatever was written into an address, and then judging it.

Two functions and one rule: **normalise, then test**. The alternative -
testing the text, or testing one family's ranges against a literal of the
other family - is how `http://[::ffff:169.254.169.254]/` reaches a metadata
endpoint through a check that was written for IPv4.

Three spellings have to survive the parse, because a resolver would accept
all of them and so must a check that runs before one:

- `127.0.0.1` - the ordinary form, which `ipaddress` handles;
- `127.1`, `2130706433`, `0x7f.0.0.1` - the short, decimal and hex forms,
  which `ipaddress` refuses and `inet_aton` accepts, exactly as the C
  resolver does. Without `inet_aton` these fall through to "it must be a
  hostname", and the check then depends on what the resolver does with them.
  It does the right thing, but a security control should not be relying on
  a correct accident one layer down.
- `::ffff:10.0.0.1` and `64:ff9b::a00:1` - the same IPv4 destination written
  as IPv6. `normalise` unwraps both, so all of these report the same
  address, which is what makes "recognised as the same blocked destination"
  literally true rather than merely equivalent in outcome.

`is_blocked` tests the literal **and** the normalised form, and refuses if
either is blocked. That matters for NAT64: `64:ff9b::8.8.8.8` normalises to a
public address, and is still refused, because the whole `64:ff9b::/96` range
is blocked - a NAT64 prefix is a route into someone's network regardless of
what is embedded in it.
"""

from __future__ import annotations

import socket
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import Final

from agentworld.infra.net.policy import BLOCKED_IPV4, BLOCKED_IPV6, EgressPolicy

Address = IPv4Address | IPv6Address

#: Parsed once at import. The tables are tuples of (cidr, description) so the
#: tests can name the row that refused an address; the networks are what the
#: membership test needs.
_BLOCKED_V4: Final[tuple[IPv4Network, ...]] = tuple(IPv4Network(cidr) for cidr, _ in BLOCKED_IPV4)
_BLOCKED_V6: Final[tuple[IPv6Network, ...]] = tuple(IPv6Network(cidr) for cidr, _ in BLOCKED_IPV6)

#: The NAT64 well-known prefix and the local-use one. An address inside
#: either embeds an IPv4 address in its last 32 bits.
_NAT64: Final = (IPv6Network("64:ff9b::/96"), IPv6Network("64:ff9b:1::/48"))


def parse_literal(host: str) -> Address | None:
    """The address `host` is, or None if it is a name.

    Returns None rather than raising: "this is a hostname" is the ordinary
    case, not a failure, and the caller's next step is to resolve it.
    """
    text = host.strip()
    if not text:
        return None
    # A bracketed IPv6 literal, as it appears in a URL authority.
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ip_address(text)
    except ValueError:
        pass
    # The short, decimal and hex IPv4 forms. `inet_aton` accepts what the C
    # resolver accepts, which is the behaviour being defended against.
    try:
        packed = socket.inet_aton(text)
    except OSError:
        return None
    return IPv4Address(packed)


def normalise(address: Address) -> Address:
    """The destination this address really names.

    IPv4-mapped and NAT64 addresses are rewritten to the IPv4 address they
    carry. Everything else is returned unchanged. Identity only - blocking
    decisions use `is_blocked`, which also looks at the original.
    """
    if not isinstance(address, IPv6Address):
        return address
    if (mapped := address.ipv4_mapped) is not None:
        return mapped
    for prefix in _NAT64:
        if address in prefix:
            # The embedded IPv4 address is the low 32 bits in both prefixes.
            return IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def is_blocked(address: Address, policy: EgressPolicy) -> bool:
    """Whether the policy refuses this address.

    Every form the address can be written in is tested, and the policy's
    `allowed_networks` only excuses the form it actually names. The order
    matters and the earlier version had it wrong: consulting the allow-list
    first and returning on a match turns the deployment escape hatch into a
    bypass. A deployment that allows `64:ff9b::/96` because it really does
    sit behind NAT64 would then accept `64:ff9b::a00:1`, which is
    `10.0.0.1` wearing a prefix the operator permitted for a different
    reason.

    So: refuse if *any* form of this address is blocked and is not itself
    allowed. Allowing the wrapper does not allow what is inside it.
    """
    return any(
        _in_blocked_table(candidate) and not _is_allowed_by(candidate, policy)
        for candidate in {address, normalise(address)}
    )


def blocking_rule(address: Address) -> str | None:
    """Which table row refuses this address, for the server-side log.

    Never for a caller. "Refused: 169.254.0.0/16 (link-local)" tells an
    operator what happened in one line and tells an attacker which of their
    guesses was interesting.
    """
    for candidate in (address, normalise(address)):
        if isinstance(candidate, IPv4Address):
            for cidr, why in BLOCKED_IPV4:
                if candidate in IPv4Network(cidr):
                    return f"{cidr} ({why})"
        else:
            for cidr, why in BLOCKED_IPV6:
                if candidate in IPv6Network(cidr):
                    return f"{cidr} ({why})"
    return None


def _is_allowed_by(address: Address, policy: EgressPolicy) -> bool:
    return any(
        address in allowed for allowed in policy.allowed_networks if _same_family(address, allowed)
    )


def _in_blocked_table(address: Address) -> bool:
    if isinstance(address, IPv4Address):
        return any(address in network for network in _BLOCKED_V4)
    return any(address in network for network in _BLOCKED_V6)


def _same_family(address: Address, network: IPv4Network | IPv6Network) -> bool:
    return address.version == network.version


def network(cidr: str) -> IPv4Network | IPv6Network:
    """`ip_network`, re-exported so callers need not import `ipaddress`.

    `EgressPolicy.allowed_networks` is the one field a deployment is expected
    to set, and making that require a second import is friction in the place
    where friction leads to someone passing a string and silently getting a
    policy that allows nothing.
    """
    return ip_network(cidr)
