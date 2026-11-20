"""Build identifier sent as ``client_info.build``.

This MUST be a constant in the loaded code, never computed at runtime from
git or from disk. Core's ``normalize_build/1`` comment (runtime_channel.ex)
explains why: a value read at join time reports what is CHECKED OUT, not what
is RUNNING, so it would report the new sha for a process that never restarted.

A Python plugin has no compile step, so the install step is the build step:
the installer overwrites this file with the commit sha it installed. An
un-stamped checkout reports ``"unstamped"``, which is honest and non-empty
(core drops an empty build to nil).
"""

BUILD = "unstamped"
