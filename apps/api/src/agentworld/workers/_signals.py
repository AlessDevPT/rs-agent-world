"""Stopping a worker loop politely.

Shared by both worker processes because they want exactly the same thing: let
the unit of work in progress finish, then return. One implementation rather
than two, for the reason `infra/net` gives for one HTTP client - one place to
review and one place that can be wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal


def install_stop_handlers(stopping: asyncio.Event) -> None:
    """Ask the loop to finish its current work and return.

    Best-effort by design. `add_signal_handler` is not implemented on
    Windows, where a developer running this in a terminal gets
    KeyboardInterrupt instead - the same outcome by a different route, which
    each entry point turns into exit 0.

    Setting an event rather than cancelling anything: a cancelled worker
    mid-dispatch would abandon a held lease, and for a consequential call it
    would abandon it with `side_effect_started_at` set, which is the state
    that costs a human a decision. The loops check this between units of
    work, never inside one.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopping.set)
