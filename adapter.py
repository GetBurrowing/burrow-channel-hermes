"""The adapter lives in ``hermes_suite_channel/adapter.py`` so it is importable
(and tested) as a normal package; this module keeps the documented plugin
layout (plugin.yaml, __init__.py, adapter.py) of hermes-agent's
gateway/platforms/ADDING_A_PLATFORM.md."""

from .hermes_suite_channel.adapter import SuiteAdapter, register

__all__ = ["SuiteAdapter", "register"]
