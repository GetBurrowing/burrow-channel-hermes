"""Hermes plugin entry point (the plugin directory is the repository root).

Hermes imports this file as ``hermes_plugins.<slug>`` with the repository root
as its package path (hermes_cli/plugins_loader.py ``_load_directory_module``),
so the relative import resolves the ``hermes_suite_channel`` package next to it.

The ``__package__`` guard exists for pytest only: pytest imports a rootdir
``__init__.py`` as a top-level module with no parent package, where a relative
import cannot resolve. Under Hermes ``__package__`` is always set.
tests/test_plugin_load.py loads this directory the way Hermes does and asserts
``register`` is exported, so the guard cannot hide a broken entry point.
"""

if __package__:
    from .hermes_suite_channel.adapter import register

    __all__ = ["register"]
