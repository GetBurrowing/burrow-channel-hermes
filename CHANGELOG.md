# Changelog

## v0.3.0

- Burrow names. Every env setting is read as `BURROW_<X>` first and the deprecated `SUITE_<X>` second, with one warning per old name used (the list is `ENV_FALLBACKS` in `hermes_suite_channel/config.py`). An install whose `.env` holds only `SUITE_` names keeps working after `update.sh`, unedited.
- install.sh writes every key under both names, so a downgrade to v0.2.0 still connects; uninstall.sh removes both spellings and nothing else. `HERMES_BURROW_CHANNEL_REPO` is read before `HERMES_SUITE_CHANNEL_REPO`.
- The allowlist, allow-all and cron home-channel names Hermes reads itself are registered as the spelling the host sets (see README "Env names").
- `plugin.yaml` no longer declares `requires_env`, so `hermes plugins install` prompts for neither spelling; both are listed under `optional_env`.
- Display text says Burrow (platform label, plugin description, log lines, agent hint). Unchanged, because the desktop app and agents key on them: plugin name `startup-suite-platform`, platform `startup_suite`, MCP server `startup-suite`, the token file name, `.suite-channel/`, the installer result lines, `client_info.product` `hermes_channel`, and the `<suite-untrusted source="startup-suite">` envelope.

## v0.2.0

- `HERMES_SUITE_CHANNEL_REPO` env override: install.sh and update.sh can clone from a fork or mirror instead of the default repository.
- Org-neutral hints: install/update messages and the README no longer hard-code an org.

Also first tagged release of earlier unreleased work: `reply_to` forwarded as `in_reply_to_message_id` (#2), interpreter resolved through a `#!/bin/sh` launcher with `--python` (#3), and `SUITE_TRUSTED_USER_IDS` trusted principals (#5).
