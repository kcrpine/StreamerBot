"""Test package.

This file is load-bearing. Without it, `unittest discover` skips the whole
tests/ tree: it walks directories only where they form importable packages, so
tests/deployment/ was silently never collected, in CI as well as locally.
"""
