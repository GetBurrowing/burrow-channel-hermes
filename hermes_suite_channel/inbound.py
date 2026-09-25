"""Reading one ``attention`` payload into the fields the adapter needs.

Pure functions, no Hermes imports, so the reading is testable on its own.

Payload shapes (core, origin/main):

- chat path: ``AgentResponder`` builds ``%{signal: build_external_signal(..),
  message: %{content, author, attachments}, history, context, tools}``. The
  signal carries ``reason``, ``space_id``, ``message_id`` and, in an
  execution space, ``task_id`` / ``task_status``, plus ``agent_id`` (the
  RECEIVING agent). ``message`` carries NO ``author_id`` today.
- system_event path: ``build_system_event_signal/2`` sends no ``message`` key.
  The rendered prompt is at ``signal.prompt``; correlation ids are lifted to
  the signal ROOT (``lift_correlation_ids/2``) and also left in
  ``signal.metadata``; the space a dispatch names is in ``metadata.space_id``
  ONLY, deliberately never at the root.

Correlation ids are read from the signal ROOT ONLY. Because core dual-writes
them, that is behaviour-preserving against every payload core sends today.
The one metadata read is the REPLY TARGET: a system_event names its space in
``metadata.space_id`` alone, so ``reply_space_id`` falls back to it, as
claude-code-suite-channel does ("replying into a metadata-named space is
safe"). The header's ``space_id`` is the root value only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Scalars read from the signal root into the envelope header and event metadata.
ROOT_KEYS: tuple[str, ...] = (
    "space_id",
    "message_id",
    "task_id",
    "task_status",
    "plan_id",
    "stage_id",
    "project_id",
    "intent_id",
    "reason",
    "responsibility_key",
    "feature",
    "trigger_kind",
    "agent_id",
)


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Attention:
    signal: dict[str, Any]
    ids: dict[str, str]  # ROOT_KEYS present and non-blank at the signal root
    reply_space_id: str  # root space_id, else metadata.space_id, else ""
    author: str
    author_id: str
    text: str  # message.content, else signal.prompt, else ""
    has_message: bool
    raw: Mapping[str, Any] = field(repr=False)

    @property
    def body(self) -> str:
        """The body the agent sees: the text, or a placeholder naming what fired."""
        return self.text if self.text.strip() else describe_bodyless_dispatch(self.ids)

    @property
    def actionable(self) -> bool:
        """Mirrors the sibling's drop guard: no body AND nothing to correlate is
        a presence sweep or a reconnect echo, not work."""
        return bool(
            self.text.strip()
            or self.ids.get("task_id")
            or self.ids.get("message_id")
            or self.ids.get("responsibility_key")
        )

    def header(self) -> dict[str, str]:
        return {
            "space_id": self.ids.get("space_id", ""),
            "message_id": self.ids.get("message_id", ""),
            "task_id": self.ids.get("task_id", ""),
            "reason": self.ids.get("reason", ""),
            "author": self.author,
        }


def describe_bodyless_dispatch(ids: Mapping[str, str]) -> str:
    """Placeholder for a dispatch with no rendered prompt, as the sibling's
    ``describeBodylessDispatch``: names what fired, never interpolates a
    missing value."""
    what = ids.get("responsibility_key") or ids.get("reason") or "unidentified responsibility"
    how = f" ({ids['trigger_kind']})" if ids.get("trigger_kind") else ""
    return f"System dispatch: {what}{how}, no rendered prompt."


def parse_attention(payload: Mapping[str, Any]) -> Attention:
    signal = _dict(payload.get("signal"))
    message_raw = payload.get("message")
    message = _dict(message_raw)
    ids = {k: v for k in ROOT_KEYS if (v := _s(signal.get(k)))}
    reply_space = ids.get("space_id") or _s(_dict(signal.get("metadata")).get("space_id"))

    # `message.content`, else `signal.prompt`: the `system_event_prompt`
    # feature declared in the handshake. JS `??` semantics, as the sibling:
    # a present-but-empty content does NOT fall through to the prompt.
    content = message.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = _s(signal.get("prompt"))

    return Attention(
        signal=signal,
        ids=ids,
        reply_space_id=reply_space,
        author=_s(message.get("author")),
        author_id=_s(message.get("author_id")),
        text=text,
        has_message=isinstance(message_raw, dict),
        raw=payload,
    )
