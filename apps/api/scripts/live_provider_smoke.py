#!/usr/bin/env python3
"""One real call to a real provider, run by hand.

**Not a test, and deliberately not under `tests/`.** Q3 asks for deterministic
mocked or recorded responses in ordinary CI, no API key required there, and a
manually initiated live smoke test kept separate. A pytest file guarded by
`skipif` would satisfy the first two and break a third thing: CI asserts the
suite reports *zero* skips, and a conditionally-skipped live test is a skip.
So this is a script. It cannot be collected, cannot be skipped, and cannot
quietly stop being run - because nothing was ever running it.

    export ANTHROPIC_API_KEY=sk-ant-...
    apps/api/.venv/bin/python apps/api/scripts/live_provider_smoke.py

On Windows:

    $env:ANTHROPIC_API_KEY = "sk-ant-..."
    apps\\api\\.venv\\Scripts\\python.exe apps\\api\\scripts\\live_provider_smoke.py

It prints what came back and exits non-zero if anything about the round trip
was wrong. What it is actually checking is the half that a local server
cannot: that the real endpoint resolves to an address the egress policy
allows, that its certificate verifies through the pinned connection with the
original SNI, that the request shape is one the real API accepts, and that
the response shape is one this parser reads. Everything else is covered
deterministically in `tests/unit/test_providers.py`.

The key is read from the environment and never printed, including in a
traceback: a failure prints the failure kind and the reason, both of which
are written to carry no credential.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentworld.infra import net, providers
from agentworld.infra.providers import anthropic, registry

#: Small on purpose. This is a round-trip check, not a benchmark, and it
#: spends real money every time it runs.
MODEL = os.environ.get("AGENTWORLD_SMOKE_MODEL", "claude-opus-5")
PROMPT = "Reply with exactly the word: pong"


async def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set. Nothing was called.", file=sys.stderr)
        return 2

    # The default policy, deliberately: if the real endpoint does not pass
    # the same egress rules a tool call would, that is the finding.
    async with net.GuardedClient() as client:
        provider = registry.build(anthropic.SLUG, client, api_key=api_key)
        request = providers.CompletionRequest(
            model=MODEL,
            messages=[providers.Message(role=providers.Role.USER, content=PROMPT)],
            max_output_tokens=16,
            temperature=0.0,
        )
        try:
            completion = await provider.complete(request)
        except providers.ProviderError as exc:
            print(f"FAILED: {exc.failure.value} ({exc.reason})", file=sys.stderr)
            print(f"retryable: {exc.failure.is_retryable}", file=sys.stderr)
            return 1

    print(f"model        {completion.model}")
    print(f"stop_reason  {completion.stop_reason.value}")
    print(f"text         {completion.text!r}")
    print(f"tokens       in={completion.usage.input_tokens} out={completion.usage.output_tokens}")

    problems = []
    if not completion.text.strip():
        problems.append("the completion carried no text")
    if completion.usage.input_tokens <= 0 or completion.usage.output_tokens <= 0:
        problems.append("token counts were not both positive")
    if completion.stop_reason is providers.StopReason.OTHER:
        problems.append("the stop reason did not map to a known member")
    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
