"""loopkit — the engine behind the loop-on-issue plugin.

Standard library only, and targeted at **Python 3.9**: macOS ships
`/usr/bin/python3` as 3.9.6, and a plugin that cannot run on a stock Mac is not
distributable. That floor is why configuration is JSON rather than TOML
(`tomllib` arrived in 3.11) and why `from __future__ import annotations` sits at
the top of every module.
"""

__version__ = "0.1.0"
