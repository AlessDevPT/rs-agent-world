"""The WebSocket gateway, and the two halves that serve it.

- `frames.py` - the one mapping from a committed `events` row to client
  frames, used by the live path and the replay path both.
- `publisher.py` - reads committed rows from a per-project position and hands
  their frames to the bus, after the mutation's transaction has committed
  (P9, P10). Hosted by the worker on the `redis` bus and by the API process on
  `inprocess`; the module says why.
- `gateway.py` - the socket: snapshot, deltas, `resume`, `resync_required`,
  ping/pong, and a bounded queue per connection.

The ticket route that opens it is `api/v1/realtime.py`, because it is an
ordinary authorized control-plane call and belongs with the others.
"""
