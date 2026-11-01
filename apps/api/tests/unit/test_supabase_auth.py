"""Supabase JWT verification.

Real RSA keys and real signatures throughout - the JWKS endpoint is faked,
the cryptography is not. A test that stubbed out `jwt.decode` would assert
only that the code calls a function, which is not the part that can be wrong.

`docs/05-ROADMAP.md` asks for "a JWT signed by the wrong key, with the wrong
audience, or expired - all 401". Those are here, plus the algorithm-confusion
attack, the key-rotation path and its abuse as a fetch amplifier.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agentworld.domain.errors import AuthenticationFailedError, DependencyUnavailableError
from agentworld.infra.auth.supabase import SupabaseAuthProvider

JWKS_URL = "https://project.supabase.co/auth/v1/.well-known/jwks.json"
AUDIENCE = "authenticated"
ISSUER = "https://project.supabase.co/auth/v1"
SUBJECT = uuid.uuid4()


class Keypair:
    """An RSA keypair plus its JWKS representation."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem_public = (
            self.private.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

    def jwk(self) -> dict[str, Any]:
        raw = jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**raw, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def token(self, **overrides: Any) -> str:
        now = dt.datetime.now(dt.UTC)
        claims: dict[str, Any] = {
            "sub": str(SUBJECT),
            "email": "agent-operator@example.com",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=1)).timestamp()),
            "user_metadata": {"full_name": "Ada Lovelace", "avatar_url": "https://x/a.png"},
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


class FakeJwks:
    """A JWKS endpoint that counts requests and can be made to fail."""

    def __init__(self, *keypairs: Keypair) -> None:
        self.keypairs = list(keypairs)
        self.requests = 0
        self.status = 200

    def transport(self) -> httpx.MockTransport:
        def handle(_request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "nope"})
            return httpx.Response(200, json={"keys": [kp.jwk() for kp in self.keypairs]})

        return httpx.MockTransport(handle)

    def provider(self, **overrides: Any) -> SupabaseAuthProvider:
        options: dict[str, Any] = {
            "jwks_url": JWKS_URL,
            "audience": AUDIENCE,
            "issuer": ISSUER,
            "client": httpx.AsyncClient(transport=self.transport()),
        }
        options.update(overrides)
        return SupabaseAuthProvider(**options)


@pytest.fixture
def signing_key() -> Keypair:
    return Keypair("key-1")


@pytest.fixture
def jwks(signing_key: Keypair) -> FakeJwks:
    return FakeJwks(signing_key)


# ------------------------------------------------------------- the happy path ---


async def test_a_valid_token_verifies(jwks: FakeJwks, signing_key: Keypair) -> None:
    provider = jwks.provider()
    identity = await provider.verify(signing_key.token())

    assert identity.subject == SUBJECT
    assert identity.email == "agent-operator@example.com"
    assert identity.display_name == "Ada Lovelace"
    assert identity.avatar_url == "https://x/a.png"
    await provider.close()


async def test_the_key_set_is_fetched_once_for_many_tokens(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """Verification is local. A per-request fetch would put Supabase on the
    critical path of every authenticated call."""
    provider = jwks.provider()
    for _ in range(5):
        await provider.verify(signing_key.token())
    assert jwks.requests == 1
    await provider.close()


async def test_a_token_without_optional_metadata_still_verifies(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    provider = jwks.provider()
    identity = await provider.verify(signing_key.token(user_metadata=None))
    assert identity.display_name is None
    await provider.close()


# ----------------------------------------------------------------- rejections ---


async def test_a_token_signed_by_the_wrong_key_is_rejected(jwks: FakeJwks) -> None:
    """Same kid, different key: a forger who copied a real header."""
    impostor = Keypair("key-1")
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(impostor.token())
    await provider.close()


async def test_an_expired_token_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(exp=int(past.timestamp())))
    await provider.close()


async def test_a_token_with_no_expiry_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    """A token that never expires is a permanent credential."""
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(exp=None))
    await provider.close()


async def test_the_wrong_audience_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    """A token minted for another application must not work here."""
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(aud="some-other-app"))
    await provider.close()


async def test_the_wrong_issuer_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(iss="https://evil.example.com/auth/v1"))
    await provider.close()


async def test_a_token_with_no_email_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    """Anonymous and phone-only identities cannot be mirrored: `users.email`
    is NOT NULL and is how members are invited."""
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(email=None))
    await provider.close()


async def test_a_subject_that_is_not_a_uuid_is_rejected(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """`users.id` is the subject verbatim, so it has to be a uuid."""
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(signing_key.token(sub="not-a-uuid"))
    await provider.close()


@pytest.mark.parametrize("garbage", ["", "   ", "not.a.token", "a.b", "a.b.c.d"])
async def test_malformed_tokens_are_rejected(jwks: FakeJwks, garbage: str) -> None:
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(garbage)
    assert jwks.requests == 0, "obvious junk should not cost a network call"
    await provider.close()


async def test_every_rejection_carries_the_same_message(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """A credential endpoint that explains itself is an enumeration oracle.

    Expired, wrong audience, forged and malformed must be one answer to the
    caller; the distinguishing detail goes to the log.
    """
    provider = jwks.provider()
    messages = set()
    for token in (
        signing_key.token(aud="other"),
        signing_key.token(exp=1),
        Keypair("key-1").token(),
        "garbage",
    ):
        with pytest.raises(AuthenticationFailedError) as caught:
            await provider.verify(token)
        messages.add(caught.value.detail)
    assert len(messages) == 1
    await provider.close()


async def test_a_401_carries_a_bearer_challenge(jwks: FakeJwks, signing_key: Keypair) -> None:
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError) as caught:
        await provider.verify(signing_key.token(exp=1))
    assert caught.value.challenge is not None
    assert caught.value.challenge.startswith("Bearer")
    await provider.close()


# ------------------------------------------------------- algorithm confusion ---


async def test_an_hs256_token_signed_with_the_public_key_is_rejected(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """The classic JWT confusion attack.

    The attacker takes the *public* key - which is public - and uses it as an
    HMAC secret, setting `alg: HS256`. A verifier that trusts the token's own
    header then validates it happily, because it has the "secret" too. Fixed
    algorithm lists are the entire defence, which is why this is tested
    rather than assumed.
    """
    # Built by hand: PyJWT's own `encode` refuses to use a PEM as an HMAC
    # secret, which is a sensible guard for honest callers and no obstacle at
    # all to an attacker with a base64 encoder.
    forged = _hs256_token_signed_with(
        signing_key.pem_public,
        kid=signing_key.kid,
        claims={
            "sub": str(SUBJECT),
            "email": "attacker@example.com",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
    )
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(forged)
    await provider.close()


async def test_an_unsigned_token_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    """`alg: none`, the other half of the same family."""
    forged = jwt.encode(
        {
            "sub": str(SUBJECT),
            "email": "attacker@example.com",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
        headers={"kid": signing_key.kid},
    )
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(forged)
    await provider.close()


async def test_a_token_with_no_kid_is_rejected(jwks: FakeJwks, signing_key: Keypair) -> None:
    unkeyed = jwt.encode(
        {
            "sub": str(SUBJECT),
            "email": "a@example.com",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        signing_key.private,
        algorithm="RS256",
    )
    provider = jwks.provider()
    with pytest.raises(AuthenticationFailedError):
        await provider.verify(unkeyed)
    await provider.close()


# ------------------------------------------------------------- key rotation ---


async def test_a_rotated_key_is_picked_up_without_a_restart(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    provider = jwks.provider()
    await provider.verify(signing_key.token())
    assert jwks.requests == 1

    rotated = Keypair("key-2")
    jwks.keypairs.append(rotated)

    identity = await provider.verify(rotated.token())
    assert identity.subject == SUBJECT
    assert jwks.requests == 2, "an unknown kid should prompt exactly one refetch"
    await provider.close()


async def test_forged_key_ids_cannot_be_used_to_hammer_supabase(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """An unauthenticated caller chooses `kid`, so a refetch per unknown kid
    would turn a stream of forged tokens into an amplifier aimed at the
    identity provider. The cooldown caps it."""
    provider = jwks.provider(refetch_cooldown_seconds=60.0)
    await provider.verify(signing_key.token())
    assert jwks.requests == 1

    for index in range(20):
        with pytest.raises(AuthenticationFailedError):
            await provider.verify(Keypair(f"forged-{index}").token())

    assert jwks.requests == 2, "one refetch for the first unknown kid, then the cooldown"
    await provider.close()


async def test_a_zero_cooldown_still_refetches_per_unknown_kid(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """Sanity check that the cap above comes from the cooldown and not from
    the refetch being broken altogether."""
    provider = jwks.provider(refetch_cooldown_seconds=0.0)
    await provider.verify(signing_key.token())
    for index in range(3):
        with pytest.raises(AuthenticationFailedError):
            await provider.verify(Keypair(f"forged-{index}").token())
    assert jwks.requests > 2
    await provider.close()


# ------------------------------------------------------------- availability ---


async def test_an_unreachable_key_set_with_a_cold_cache_is_a_503(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """Not a 401. The caller's credential may be perfectly good; it is this
    server that cannot check it, and a retry is the right advice."""
    jwks.status = 500
    provider = jwks.provider()
    with pytest.raises(DependencyUnavailableError):
        # A well-formed token: a malformed one is refused before any fetch is
        # attempted, which is deliberate - no network call for obvious junk.
        await provider.verify(signing_key.token())
    await provider.close()


async def test_an_unreachable_key_set_with_a_warm_cache_keeps_working(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """The keys are still the right keys, just older than we would like.

    Refusing every request because the JWKS endpoint is briefly down would
    make an identity-provider blip into a full outage.
    """
    provider = jwks.provider(cache_ttl_seconds=0.0, refetch_cooldown_seconds=0.0)
    await provider.verify(signing_key.token())

    jwks.status = 503
    identity = await provider.verify(signing_key.token())
    assert identity.subject == SUBJECT
    await provider.close()


async def test_an_unusable_key_in_the_set_does_not_break_the_others(
    signing_key: Keypair,
) -> None:
    """One odd entry in Supabase's response must not lock every user out."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "keys": [
                    {"kid": "broken", "kty": "RSA", "alg": "RS256", "n": "!!!", "e": "AQAB"},
                    {"kid": "unsupported-alg", "kty": "oct", "alg": "HS256", "k": "abc"},
                    signing_key.jwk(),
                ]
            },
        )

    provider = SupabaseAuthProvider(
        jwks_url=JWKS_URL,
        audience=AUDIENCE,
        issuer=ISSUER,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    identity = await provider.verify(signing_key.token())
    assert identity.subject == SUBJECT
    await provider.close()


async def test_a_key_set_with_nothing_usable_is_a_503(signing_key: Keypair) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": []})

    provider = SupabaseAuthProvider(
        jwks_url=JWKS_URL,
        audience=AUDIENCE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(DependencyUnavailableError):
        await provider.verify(signing_key.token())
    await provider.close()


async def test_clock_skew_within_the_leeway_is_tolerated(
    jwks: FakeJwks, signing_key: Keypair
) -> None:
    """A token that expired two seconds ago is almost certainly a clock
    difference, not an attack; one that expired an hour ago is not."""
    just_expired = int((dt.datetime.now(dt.UTC) - dt.timedelta(seconds=2)).timestamp())
    provider = jwks.provider(leeway_seconds=10.0)
    identity = await provider.verify(signing_key.token(exp=just_expired))
    assert identity.subject == SUBJECT
    await provider.close()


def _hs256_token_signed_with(secret: str, *, kid: str, claims: dict[str, Any]) -> str:
    """A JWT with `alg: HS256`, HMAC-signed with `secret`.

    Hand-rolled because the attack it models is hand-rolled: the forger
    treats a published public key as a shared secret and hopes the verifier
    takes the algorithm from the token.
    """
    import base64
    import hashlib
    import hmac
    import json

    def segment(payload: dict[str, Any]) -> bytes:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = segment({"alg": "HS256", "typ": "JWT", "kid": kid}) + b"." + segment(claims)
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
