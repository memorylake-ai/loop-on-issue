#!/bin/sh
# Run the plugin's test suite. Standard library only — no pip install required.
#
# Discovery runs with the tests directory as cwd on purpose: pointing
# `unittest discover` at it via -t from elsewhere makes it try to import the
# directory as a top-level module, which fails since it is deliberately not a
# package (that is also what puts tests/ on sys.path so `import _bootstrap`
# resolves).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here/tests"
exec "${PYTHON:-python3}" -m unittest discover -s . "$@"
