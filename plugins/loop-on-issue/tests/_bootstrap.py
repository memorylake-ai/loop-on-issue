"""Put the plugin's `scripts/` directory on sys.path.

Imported for its side effect by every test module, so tests run identically
under `python3 -m unittest discover` and when a single file is executed
directly. `unittest discover` inserts the start directory (this one) into
sys.path, which is what makes the plain `import _bootstrap` resolve.
"""

import os
import sys

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
