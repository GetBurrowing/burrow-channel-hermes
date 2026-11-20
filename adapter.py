"""The adapter lives in ``hermes_burrow_channel/adapter.py`` so it is importable
(and tested) as a normal package; this module keeps the documented plugin
layout (plugin.yaml, __init__.py, adapter.py) of hermes-agent's
gateway/platforms/ADDING_A_PLATFORM.md."""

from .hermes_burrow_channel.adapter import BurrowAdapter, register

__all__ = ["BurrowAdapter", "register"]
