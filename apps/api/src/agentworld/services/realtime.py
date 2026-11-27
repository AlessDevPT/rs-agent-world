"""WebSocket tickets: minting one, and spending one.

`03-API.md` §6 makes the reason explicit. A browser cannot set a header on a
WebSocket, so the only place a credential could ride is the query string -
where it reaches every access log and proxy on the way. The exchange is
therefore: the client presents its JWT to an ordinary authorized route, gets
back a ticket that is worth sixty seconds and one connection, and spends it on
the socket.

**What a ticket is.** `{workspace}.{id}.{secret}` - two uuids in hex and 32
random bytes, base64url. The secret is the credential; the two ids are
addressing.

The workspace is in there for a structural reason rather than convenience. The
gateway is handed a ticket and nothing else: no token, no memberships, no
visibility set - and `ws_tickets` carries a row-level security policy keyed on
exactly that set, so without the workspace the row would be invisible to the
query that is trying to find it. That is the same circle `api_keys` resolves by
carrying *no policy at all*, and this is the cheaper way out of it: the string
names the tenant, so the set can be established before the lookup, and the
table keeps its policy.

Naming it is safe because the set is only a read filter. A forged workspace
id widens visibility to a tenant whose ticket rows the forger still cannot
name - redemption needs the secret's digest - and every value the connection
is then scoped by is read back from the row, never taken from the string. A
mismatch is indistinguishable from a ticket that never existed, which is the
answer R1 asks for.

**One action, checked once.** Minting authorizes `realtime.subscribe` at
`viewer`; spending does not re-authorize, because there is nobody to ask - and
nothing to ask about, since the ticket records who it was minted for and the
frames never carry more than that role's existing reads. The authorization
happens where a credential exists, which is the only place it can mean
anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from agentworld.domain.errors import AuthenticationFailedError, NotFoundError
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext

#: Bytes of entropy in a ticket's secret half. 32, like an API key's, and for
#: the same reason: it is the whole credential and there is no second factor.
SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class MintedTicket:
    """What the issuing route returns. The string appears exactly once."""

    ticket: str
    expires_at: dt.datetime
    project_id: UUID


@dataclass(frozen=True, slots=True)
class StreamGrant:
    """What a spent ticket authorises. Every field came from the row."""

    workspace_id: UUID
    project_id: UUID
    user_id: UUID


@dataclass(frozen=True, slots=True)
class _Parsed:
    workspace_id: UUID
    ticket_id: UUID
    secret: str


def digest(secret: str) -> str:
    """SHA-256 of a ticket secret, hex.

    Not Argon2, and the difference is the threat rather than an oversight. An
    API key is a long-lived bearer token whose stolen digest must stay
    uncrackable for as long as the key lives, so it is worth tens of
    milliseconds. A ticket is 32 uniform random bytes that expire in a minute
    and cannot be used twice: there is no low-entropy input to guess, so the
    work factor would protect nothing and would be paid on every connection.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse(presented: str) -> _Parsed | None:
    """Split a ticket, or None if it is not one.

    Total, and silent about *why* - a malformed ticket, a well-formed one for
    another project and an expired one all have to produce the same refusal,
    or the refusal becomes an oracle.
    """
    parts = presented.split(".")
    if len(parts) != 3:
        return None
    workspace_raw, ticket_raw, secret = parts
    if not secret:
        return None
    try:
        return _Parsed(UUID(workspace_raw), UUID(ticket_raw), secret)
    except ValueError:
        return None


class RealtimeTicketService:
    """Mint a ticket for a project, and spend one on a connection."""

    def __init__(self, factory: UnitOfWorkFactory, *, ttl_seconds: int = 60) -> None:
        self._factory = factory
        self._ttl = dt.timedelta(seconds=ttl_seconds)

    async def mint(
        self, principal: UserPrincipal, context: CallContext, project_id: UUID
    ) -> MintedTicket:
        """Issue a single-use ticket for this project's stream.

        The workspace is resolved before the check, under a session that can
        see only the caller's own workspaces, so a stranger's project id has
        no row and 404s without any authorization having to notice - the
        shape every project-addressed route here uses.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.REALTIME_SUBSCRIBE, Resource(workspace_id))

            ticket_id = uuid4()
            secret = secrets.token_urlsafe(SECRET_BYTES)
            expires_at = dt.datetime.now(dt.UTC) + self._ttl

            await work.ws_tickets(TenantScope(workspace_id, project_id)).create(
                ticket_id=ticket_id,
                user_id=principal.user_id,
                secret_digest=digest(secret),
                expires_at=expires_at,
            )
            # Audited like any other credential issue. A ticket is short-lived
            # and narrow, but it is still a key to a project's live state, and
            # "who opened a stream on this project" is a question an owner is
            # entitled to be able to answer.
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="realtime.ticket_minted",
                resource_type="project",
                resource_id=project_id,
                metadata={"ticket_id": str(ticket_id)},
            )
            return MintedTicket(
                ticket=f"{workspace_id.hex}.{ticket_id.hex}.{secret}",
                expires_at=expires_at,
                project_id=project_id,
            )

    async def redeem(self, presented: str, *, project_id: UUID) -> StreamGrant:
        """Spend a ticket for this project, or refuse.

        Every refusal is the same error with the same message, as the API-key
        path does: a reason would tell a caller whether the id existed, which
        project it was for, and whether it had already been used.
        """
        parsed = parse(presented)
        if parsed is None:
            raise _refused()

        now = dt.datetime.now(dt.UTC)
        # The visibility set comes from the ticket, which is what lets
        # `ws_tickets` keep its policy - see this module's docstring. The
        # scope pins the project from the *path*, so a ticket minted for
        # another project in the same workspace matches no row (R3).
        async with self._factory.begin(visible_workspaces={parsed.workspace_id}) as work:
            scope = TenantScope(parsed.workspace_id, project_id)
            redeemed = await work.ws_tickets(scope).consume(parsed.ticket_id, now=now)

        if redeemed is None:
            raise _refused()
        if not hmac.compare_digest(redeemed.secret_digest, digest(parsed.secret)):
            # The ticket has been consumed by the statement above and stays
            # consumed. A real id with the wrong secret is an attack on that
            # ticket; burning it is the right answer, and the holder can mint
            # another in one request.
            raise _refused()
        return StreamGrant(
            workspace_id=redeemed.workspace_id,
            project_id=redeemed.project_id,
            user_id=redeemed.user_id,
        )


def _refused() -> AuthenticationFailedError:
    return AuthenticationFailedError(
        "This stream needs a valid, unspent ticket from POST /v1/projects/{id}/ws-ticket",
        reason="invalid_ticket",
    )


async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
    """Which of the caller's workspaces holds this project.

    Works only because the session was opened over the caller's memberships:
    row-level security confines the lookup to projects they can already see,
    so a stranger's project id has no row and 404s.
    """
    workspace_id = await work.project_lookup.workspace_of(project_id)
    if workspace_id is None:
        raise NotFoundError("Project not found", project_id=str(project_id))
    return workspace_id
