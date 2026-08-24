"""Failure kinds, mapped onto the exit codes every caller branches on.

  0  success
  2  precondition not met — the issue moved on, someone else claimed it, there is
     nothing to do. A normal "skip this one" signal, not a failure. The swarm
     leans on this heavily; treating it as an error would turn every lost claim
     race into an escalation.
  1  an actual error — network, auth, an unexpected API response.
"""

from __future__ import annotations

SUCCESS = 0
ERROR = 1
PRECONDITION = 2


class Precondition(Exception):
    """Nothing was done and nothing is broken; the caller should move on."""
