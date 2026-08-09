"""Custom gptme tool definitions, loaded by file path (not import).

gptme's own ``init_tools(allowlist)`` accepts allowlist entries that are
either built-in tool names or paths to ``.py`` files containing a
module-level ``ToolSpec`` (``gptme.tools.base.load_from_file``). Modules in
this package are meant to be referenced by path — see
``SEARXNG_TOOL_PATH`` in ``searxng_search.py`` — never imported directly
into the harness process, so they have no dependency on this package's
``__init__`` beyond making the directory a normal Python package for
packaging purposes.
"""
