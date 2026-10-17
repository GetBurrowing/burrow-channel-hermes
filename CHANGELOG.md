# Changelog

## v0.2.0

- `HERMES_SUITE_CHANNEL_REPO` env override: install.sh and update.sh can clone from a fork or mirror instead of the default repository.
- Org-neutral hints: install/update messages and the README no longer hard-code an org.

Also first tagged release of earlier unreleased work: `reply_to` forwarded as `in_reply_to_message_id` (#2), interpreter resolved through a `#!/bin/sh` launcher with `--python` (#3), and `SUITE_TRUSTED_USER_IDS` trusted principals (#5).
