"""The one place a secret is removed from text before it is logged.

The runtime token travels in exactly one place: the query string of the
socket URL (Phoenix socket connect params). Anything that renders that URL,
or the request path the websockets library logs at DEBUG (``> GET
/runtime/ws/websocket?vsn=..&token=.. HTTP/1.1``), or an exception whose
message embeds the URI (``websockets.exceptions.InvalidURI``), carries the
token with it. Every log record this package emits goes through
``redact()`` via ``RedactingLogger``; nothing logs around it.

Two independent passes, so either one alone still hides the token:

1. any query string attached to a URL or an absolute path is replaced with
   ``?[redacted]``;
2. every known secret value is replaced literally, raw and URL-encoded.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[redacted]"

# scheme://anything-up-to-whitespace-or-quote, and bare absolute paths such
# as the request target websockets logs. Group 1 keeps the part before "?".
_URL_QUERY = re.compile(r"((?:[a-zA-Z][a-zA-Z0-9+.-]*://|/)[^\s?'\"<>]*)\?[^\s'\"<>]*")


def redact(text: object, secrets: Iterable[str | None] = ()) -> str:
    """Return ``text`` with URL query strings and every secret removed."""
    out = _URL_QUERY.sub(lambda m: f"{m.group(1)}?{REDACTED}", str(text))
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            out = out.replace(form, REDACTED)
    return out


class RedactingLogger(logging.LoggerAdapter):
    """A logger adapter that formats, then redacts, every record.

    Passed to ``websockets`` as its ``logger`` too, so the library's own DEBUG
    request line is redacted before it reaches any handler. Records are
    emitted pre-formatted with no args and no ``exc_info``: a traceback is
    rendered to text and redacted here, because a handler formatting it later
    would see the raw exception message.
    """

    def __init__(self, logger: logging.Logger, secrets: Iterable[str | None] = ()):
        super().__init__(logger, {})
        self._secrets = tuple(s for s in secrets if s)

    def log(self, level, msg, *args, exc_info=None, stack_info=False, stacklevel=1, **_kw):
        if not self.isEnabledFor(level):
            return
        try:
            text = str(msg) % args if args else str(msg)
        except (TypeError, ValueError):
            text = " ".join([str(msg), *map(str, args)])
        if exc_info:
            if exc_info is True:
                exc_info = sys.exc_info()
            elif isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
            if exc_info[0] is not None:
                text += "\n" + "".join(traceback.format_exception(*exc_info)).rstrip()
        self.logger.log(level, redact(text, self._secrets), stacklevel=stacklevel + 2)
