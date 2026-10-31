# `world/` — the isometric island

Nothing lives here yet. The island is built in **Phase 6**, deliberately after the
product is already useful without it (`docs/05-ROADMAP.md`).

The directory exists now because its boundary rules do. `eslint.config.mjs` already
enforces both halves of goal G8:

- nothing outside `world/` may import `phaser` or anything under `world/phaser/`
- `world/projector/**` may not import React, Phaser, `next/*` or the API client

Adding those rules after the code would only have documented the violations that had
already crept in.

## Planned layout

```
world/
  types.ts         WorldState, WorldEntity, AnimationIntent
  projector/       project(domain, layout) -> WorldState   (pure, unit-tested)
  layout/          deterministic tile assignment for new agents
  phaser/          Scene, sprites, isometric tilemap, effects
  bridge/          React <-> Phaser mount; one store subscription
```

## The contract

```ts
project(domain: DomainState, layout: LayoutConfig): WorldState
```

- `DomainState` is exactly what the API returns: agents, tasks, runs, recent events.
- `LayoutConfig` is presentation-only — tile coordinates, building kind, character
  sprite — stored in `world_placements`, a table no reducer or adapter touches.
- `WorldState` is tiles, entities and animation intents.

Two properties to hold on to, both testable:

1. **Every animation traces to a real event.** The projector owns no timers and invents
   no activity. A still island means the agents are genuinely idle.
2. **Deleting `world/` breaks no test outside `world/`.** That is the isolation test in
   Phase 6, and the reason the rules above exist from Phase 0.
