"""Host and SNI preservation, against a real TLS server.

`06-OUTBOUND-SECURITY.md` section 9 asks for this one "against a real TLS
test server, not a mock", and the insistence is right. Pinning rewrites the
URL's host to an IP address, and a mock cannot tell you whether the TLS
handshake then presented the original hostname or the address - which decides
whether certificate verification checks the name the caller asked for or
nothing useful at all. Getting that wrong would leave every outbound HTTPS
request verifying a certificate against an IP it just connected to, which is
a check that cannot fail and therefore is not a check.

So: a real socket, a real certificate issued for `example.test`, a real
handshake, and the server records the SNI it was given.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agentworld.infra import net

HOSTNAME = "example.test"
WRONG_HOSTNAME = "other.test"


def _certificate(tmp: Path, common_name: str) -> tuple[Path, Path]:
    """A self-signed certificate for `common_name`, and its key.

    `CA:TRUE` so the same file can be the trust anchor: a test that trusts
    its own throwaway certificate is the only way to get a real handshake
    without a real certificate authority. `verify` is operator and test
    configuration on `GuardedClient` and there is no path to it from a
    request body.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp / f"{common_name}.pem"
    key_path = tmp / f"{common_name}.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


@dataclass
class TlsServer:
    """One TLS listener on loopback that answers one request per connection.

    It echoes the request's `Host` header in the body, so the test can assert
    what the client actually sent rather than what it meant to send.
    """

    port: int
    certificate: Path
    sni_seen: list[str | None] = field(default_factory=list)


async def _serve(tmp: Path, common_name: str) -> AsyncIterator[TlsServer]:
    certificate_path, key_path = _certificate(tmp, common_name)
    state = TlsServer(port=0, certificate=certificate_path)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)

    def record_sni(sock: ssl.SSLObject, name: str | None, _context: ssl.SSLContext) -> None:
        state.sni_seen.append(name)

    context.sni_callback = record_sni  # type: ignore[assignment]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            return
        host = ""
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("host:"):
                host = line.split(":", 1)[1].strip()
        body = host.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    state.port = server.sockets[0].getsockname()[1]
    async with server:
        yield state


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[TlsServer]:
    async for server in _serve(tmp_path, HOSTNAME):
        yield server


def _policy(port: int) -> net.EgressPolicy:
    """Loopback and this one ephemeral port, both named deliberately.

    Using the escape hatch rather than a flag that disables the guard: what
    is under test is the real client with a real policy, and a policy that
    permits the test's own server is a policy a self-hoster could write.
    """
    return net.EgressPolicy(
        allowed_ports=frozenset({port}),
        allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
    )


def _resolver(address: str = "127.0.0.1") -> net.Resolver:
    async def resolve(_host: str, _port: int) -> Sequence[net.Address]:
        return [ipaddress.ip_address(address)]

    return resolve


def _trusting(certificate: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(certificate))


async def test_the_host_header_and_sni_carry_the_original_hostname(
    tls_server: TlsServer,
) -> None:
    """The acceptance criterion, end to end over a real handshake.

    The connection goes to 127.0.0.1 and the server is told it is
    `example.test` twice: once in the TLS ClientHello, which is what the
    certificate is verified against, and once in the `Host` header, which is
    what a virtual host dispatches on. Both have to be right for a pinned
    connection to reach a shared-hosting endpoint at all.
    """
    client = net.GuardedClient(
        _policy(tls_server.port),
        resolve=_resolver(),
        verify=_trusting(tls_server.certificate),
    )
    async with client:
        response = await client.request("GET", f"https://{HOSTNAME}:{tls_server.port}/thing")

    assert response.status_code == 200
    # What the server saw in the Host header, echoed back.
    assert response.content.decode() == f"{HOSTNAME}:{tls_server.port}"
    # What the server saw in the ClientHello.
    assert tls_server.sni_seen == [HOSTNAME]


async def test_the_certificate_is_verified_against_the_name_not_the_address(
    tmp_path: Path,
) -> None:
    """The check that proves the check is a check.

    The server presents a certificate for `other.test` and the client asked
    for `example.test`. If SNI and verification used the pinned address, or
    if verification were skipped because the URL host is an IP, this would
    succeed. It must fail, and it must fail as a normalised TLS failure
    rather than as a raw ssl error.
    """
    async for server in _serve(tmp_path, WRONG_HOSTNAME):
        client = net.GuardedClient(
            _policy(server.port),
            resolve=_resolver(),
            verify=_trusting(server.certificate),
        )
        async with client:
            with pytest.raises(net.OutboundError) as excinfo:
                await client.request("GET", f"https://{HOSTNAME}:{server.port}/thing")
        assert excinfo.value.failure is net.OutboundFailure.TLS_FAILURE
        # The reason is a type name, not the certificate's subject.
        assert WRONG_HOSTNAME not in excinfo.value.reason


async def test_an_untrusted_certificate_is_a_tls_failure(tls_server: TlsServer) -> None:
    """Verification is on and has no opt-out from user input."""
    client = net.GuardedClient(_policy(tls_server.port), resolve=_resolver())
    async with client:
        with pytest.raises(net.OutboundError) as excinfo:
            await client.request("GET", f"https://{HOSTNAME}:{tls_server.port}/thing")
    assert excinfo.value.failure is net.OutboundFailure.TLS_FAILURE


async def test_a_refused_connection_is_unreachable(tmp_path: Path) -> None:
    """A real closed port, so the kind comes from a real socket error.

    Paired with the blocked-destination tests: this is the other half of
    "indistinguishable", and it is the half that has to come from the
    network rather than from a constructor.
    """
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    listener.close()
    await listener.wait_closed()

    client = net.GuardedClient(_policy(port), resolve=_resolver())
    async with client:
        with pytest.raises(net.OutboundError) as excinfo:
            await client.request("GET", f"http://{HOSTNAME}:{port}/thing")

    assert excinfo.value.failure is net.OutboundFailure.UNREACHABLE
    blocked = net.OutboundError(net.OutboundFailure.BLOCKED_DESTINATION, reason="10.0.0.0/8")
    assert str(excinfo.value) == str(blocked)


async def test_a_body_over_the_limit_is_truncated(tmp_path: Path) -> None:
    """Streamed and cut, and the cut is reported rather than hidden.

    `Content-Length` is the sender's claim, so the limit is applied to what
    actually arrives: this server says 64 KiB and the policy allows 1 KiB.
    """
    payload = b"x" * 65536

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        policy = net.EgressPolicy(
            allowed_ports=frozenset({port}),
            allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
            max_response_bytes=1024,
        )
        client = net.GuardedClient(policy, resolve=_resolver())
        async with client:
            response = await client.request("GET", f"http://{HOSTNAME}:{port}/big")

    assert response.truncated is True
    assert len(response.content) == 1024


async def test_a_redirect_is_returned_and_not_followed(tmp_path: Path) -> None:
    """Section 4: never automatically, and the metadata address is why.

    The client hands the 302 back with its `Location` intact. Following it
    would mean reaching a destination the caller never named, and the first
    caller that wants hops brings its own requirements - including the
    independent validation of each one.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://169.254.169.254/latest/meta-data/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = net.GuardedClient(_policy(port), resolve=_resolver())
        async with client:
            response = await client.request("GET", f"http://{HOSTNAME}:{port}/go")

    assert response.status_code == 302
    assert response.headers["location"] == "http://169.254.169.254/latest/meta-data/"
    assert response.content == b""
