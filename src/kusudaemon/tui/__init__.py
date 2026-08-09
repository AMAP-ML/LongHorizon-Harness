"""The terminal UI (PLAN.md §11 control surface) — replaces the deleted web
view. ``state.py`` is a plain library module (no ``textual`` import, kept
gptme/textual-free so the test suite can exercise it without either
optional extra installed); ``app.py`` is the only module that imports
``textual`` (``pip install "kusudaemon[tui]"``).
"""

from __future__ import annotations
