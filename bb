#!/usr/bin/env bash
# Wrapper: startet das Tool mit dem venv-Python.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/bookkeeping.py" "$@"
