"""Supabase Auth JWT verification.

Supabase signs user tokens with an asymmetric key and publishes the public
half as a JWKS document. Verification is therefore entirely local: no call to
Supabase on the request path, so an outage of the identity provider does not
take the API down with it, and a token check costs one signature verification
rather than a network round trip.

Three things here are security-relevant rather than merely functional:

1. **The algorithm list is fixed.** Only the asymmetric algorithms Supabase
   actually uses are accepted. Deriving the algorithm from the token's own
   header is the classic JWT confusion attack: a forger sets `alg: HS256` and
   signs with the *public* key, which a lenient verifier will happily accept
   as a valid HMAC.
2. **An unknown `kid` triggers at most one refetch per cooldown.** Key
   rotation must work without a restart, so an unrecognised key id does
   prompt a refresh - but unauthenticated callers choose that `kid`, so
   without a cooldown a stream of forged tokens becomes a denial-of-service
   amplifier aimed at Supabase.
3. **Failures are indistinguishable to the caller.** Every path raises the
   same error with the same message. The specific cause is logged.
"""

from __future__ import annotations

import time
from typing import Any, Final, NoReturn
from uuid import UUID

import httpx
import jwt
from anyio import Lock

from agentworld.domain.errors import AuthenticationFailedError, DependencyUnavailableError
from agentworld.infra.auth.provider import VerifiedIdentity
from agentworld.infra.logging import get_logger

#: Supabase issues RS256 by default and ES256 for projects using
#: elliptic-curve signing keys. Symmetric algorithms are absent on purpose -
#: see the module docstring.
ALLOWED_ALGORITHMS: Final[list[str]] = ["RS256", "ES256"]

CHALLENGE: Final = 'Bearer realm="agentworld"'

_GENERIC_FAILURE: Final = "The credential presented could not be verified"

log = get_logger(__name__)


class SupabaseAuthProvider:
    """Verifies Supabase tokens against a cached JWKS document."""

    def __init__(
        self,
        *,
        jwks_url: str,
        audience: str,
        issuer: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = 600.0,
        refetch_cooldown_seconds: float = 30.0,
        leeway_seconds: float = 10.0,
    ) -> None:
        self._jwks_url = jwks_url
        self._audience = audience
        self._issuer = issuer
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self._owns_client = client is None
        self._cache_ttl = cache_ttl_seconds
        self._refetch_cooldown = refetch_cooldown_seconds
        self._leeway = leeway_seconds

        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float = 0.0
        # Tracked separately from `_fetched_at` on purpose. If the cooldown
        # were measured from the last fetch of any kind, the first unknown
        # kid after a scheduled refresh would be refused without a refetch -
        # so a key rotation would 401 every user for a whole cooldown window.
        # Measuring it from the last *forced* fetch means rotation is picked
        # up at once, while a stream of forged kids still costs one fetch per
        # window.
        self._last_forced_at: float = 0.0
        self._lock = Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def verify(self, token: str) -> VerifiedIdentity:
        key = await self._key_for(token)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    # A token with no expiry would be a permanent credential;
                    # one with no subject cannot be resolved to a user. Both
                    # are rejected rather than defaulted.
                    "require": ["exp", "sub"],
                    "verify_aud": True,
                    "verify_iss": self._issuer is not None,
                },
            )
        except jwt.PyJWTError as exc:
            self._reject("decode_failed", reason_detail=type(exc).__name__)

        return self._identity_from(claims)

    # ---------------------------------------------------------------- keys ---

    async def _key_for(self, token: str) -> jwt.PyJWK:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            self._reject("malformed_header", reason_detail=type(exc).__name__)

        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            # Checked here as well as inside `decode`: a token asking for an
            # algorithm we do not use should never reach key lookup, let alone
            # the verifier.
            self._reject("algorithm_not_allowed", algorithm=str(algorithm))

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            self._reject("missing_kid")

        await self._refresh_if_stale()
        if (key := self._keys.get(kid)) is not None:
            return key

        # Unknown key id: either a rotation we have not seen, or a forgery.
        # One refetch per cooldown covers the first without letting the second
        # turn into traffic against Supabase.
        if time.monotonic() - self._last_forced_at >= self._refetch_cooldown:
            await self._refresh(force=True)
            if (key := self._keys.get(kid)) is not None:
                return key

        self._reject("unknown_kid", kid=kid)

    async def _refresh_if_stale(self) -> None:
        if not self._keys or time.monotonic() - self._fetched_at >= self._cache_ttl:
            await self._refresh(force=False)

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            # Another request may have refreshed while this one waited, so
            # re-check inside the lock: a burst of requests causes one fetch.
            fresh = bool(self._keys) and time.monotonic() - self._fetched_at < self._cache_ttl
            if fresh and not force:
                return
            if force and time.monotonic() - self._last_forced_at < self._refetch_cooldown:
                return
            if force:
                self._last_forced_at = time.monotonic()
            try:
                response = await self._client.get(self._jwks_url)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._keys:
                    # Serving from a stale cache beats refusing every request:
                    # the keys are still the right keys, they are just older
                    # than we would like.
                    log.warning(
                        "jwks_refresh_failed_using_cache",
                        error=type(exc).__name__,
                        url=self._jwks_url,
                    )
                    return
                log.error("jwks_unavailable", error=type(exc).__name__, url=self._jwks_url)
                raise DependencyUnavailableError(
                    "The identity provider's key set is unreachable; retry shortly"
                ) from exc

            keys = _parse_jwks(payload)
            if not keys:
                log.error("jwks_empty", url=self._jwks_url)
                if not self._keys:
                    raise DependencyUnavailableError(
                        "The identity provider returned no usable signing keys"
                    )
                return

            self._keys = keys
            self._fetched_at = time.monotonic()
            log.info("jwks_refreshed", key_count=len(keys))

    # -------------------------------------------------------------- claims ---

    def _identity_from(self, claims: dict[str, Any]) -> VerifiedIdentity:
        try:
            subject = UUID(str(claims["sub"]))
        except (KeyError, ValueError):
            self._reject("subject_not_uuid")

        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            # Anonymous and phone-only Supabase identities are out of scope
            # for v1: `users.email` is NOT NULL and is how members are
            # invited, so an identity without one cannot be mirrored.
            self._reject("no_email_claim", subject=str(subject))

        raw_metadata = claims.get("user_metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        return VerifiedIdentity(
            subject=subject,
            email=email,
            display_name=_optional_str(metadata.get("full_name") or metadata.get("name")),
            avatar_url=_optional_str(metadata.get("avatar_url")),
            claims=claims,
        )

    def _reject(self, reason: str, **context: Any) -> NoReturn:
        log.info("jwt_rejected", reason=reason, **context)
        raise AuthenticationFailedError(_GENERIC_FAILURE, challenge=CHALLENGE, reason=reason)


def _parse_jwks(payload: Any) -> dict[str, jwt.PyJWK]:
    """Build a kid -> key map, skipping entries we cannot use.

    One unusable entry - a key type `cryptography` was not built with, say -
    must not invalidate the rest of the set, or a single odd key in Supabase's
    response would lock every user out.
    """
    if not isinstance(payload, dict):
        return {}
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list):
        return {}

    parsed: dict[str, jwt.PyJWK] = {}
    for entry in raw_keys:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("alg") not in ALLOWED_ALGORITHMS:
            continue
        try:
            parsed[kid] = jwt.PyJWK.from_dict(entry)
        except Exception as exc:
            log.warning("jwks_key_unusable", kid=kid, error=type(exc).__name__)
    return parsed


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
