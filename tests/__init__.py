"""Test package marker.

This file exists for one reason: without it, `tests` is only a *namespace* portion, and
Python's import machinery gives a regular package found later on `sys.path` priority over
a namespace portion found earlier. Any unrelated project on the machine that ships an
installed `tests/__init__.py` therefore shadows this directory, and `conftest.py`'s
`from tests.fakes import ...` fails before a single test is collected.

Making this a regular package pins `tests.*` to this repository.
"""

from __future__ import annotations
