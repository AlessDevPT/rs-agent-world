"""Protocol version.

This is versioned separately from the API and from the application, because it
is the contract integrators build against. The compatibility promises in
docs/03-API.md section 5 apply to every release that keeps the same major:

  - unknown event types are accepted and stored
  - unknown payload fields are preserved
  - omitted optional fields keep their meaning
  - responses may gain fields; clients ignore unknown ones
  - enums may gain members; clients render unknown ones as raw strings
"""

from __future__ import annotations

from typing import Final

PROTOCOL_VERSION: Final = "1.0"
PROTOCOL_MAJOR: Final = 1
PROTOCOL_MINOR: Final = 0
