# bus-tools/

Versioned source of the bus operator tooling. These scripts are vendored
from the live skill bins so every tooling change is committable, reviewable,
and tested in this repo (per the crew's bus-tooling-move proposal).

- `bus-send`, `bus-poll`, `bus-rooms` — thin Upstash REST wrappers used for
  daily bus operation (live copies run from the skill bin).
- `names.py` — standalone display-name resolver (stdlib only; endpoint and
  token are arguments so vault-backed wrappers can call it without repo
  config). Used by `bus-send` to post with the sender's display name.

`state/` (poll cursors) is runtime data and is intentionally NOT vendored.

`tests/test_bustools_parity.py` asserts the live skill copies are
byte-identical to these — drift fails loudly.
