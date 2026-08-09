"""The web dashboard (PLAN.md §11 control surface). ``state.py`` is a
plain library module (no ``http.server``/``textual``/``gptme`` import, so
the test suite can exercise it directly); ``server.py`` is the stdlib
``http.server`` transport mounting it, and ``static/`` is its frontend.
"""

from __future__ import annotations
