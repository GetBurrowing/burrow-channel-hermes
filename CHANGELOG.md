# Changelog

## v0.3.1

- Product-name wording only: errors, help text, docstrings, the untrusted-content preamble and the platform hint say Burrow. Tool names, parameters, identifiers (`startup-suite-platform`, `startup_suite`, `suite-untrusted`, `source="startup-suite"`) and wire behaviour are unchanged.
- The owner marker is now `[Burrow message from your owner ...]` (was `[Suite message from your owner ...]`). The plugin emits the new marker for trusted owners, and the platform hint names the new marker. An untrusted body spelling EITHER marker stays inside the `<suite-untrusted>` block (permanent test in `tests/test_trusted_principals.py`).
- Restart running gateways to load this version, AND start a new session (`/new`) in each live conversation. Hermes puts the platform hint into the session's system prompt once and persists it in the session database; a continuing session reuses those stored bytes verbatim, across gateway restarts, until a new session, a context compression, or a model/provider/working-directory change rebuilds it (hermes-agent fdec926e `agent/conversation_loop.py` `_restore_or_build_system_prompt`). Until then that session's hint names the old marker while trusted messages arrive with the new one, so owner messages there may be treated with less trust than intended. Nothing becomes more trusted: the untrusted block is unchanged.

## v0.3.0

- update.sh (and install.sh over an existing install) works on a host whose plugin is enabled. Hermes refuses to replace an enabled plugin that declares Python dependencies without a terminal user's consent ("Reinstall declined: dependency install skipped (non-interactive)"), which made update.sh (v0.2.0 and the earlier v0.3.0 work) exit 1 on an enabled host (measured at hermes-agent fdec926e in its default home and in a live profile). The installer now disables, reinstalls with `--no-enable`, and enables; any failure after the disable re-enables the plugin at the build still installed.
- Burrow names. Every env setting is read as `BURROW_<X>` first and the deprecated `SUITE_<X>` second, with one warning per old name used (the list is `ENV_FALLBACKS` in `hermes_suite_channel/config.py`). An install whose `.env` holds only `SUITE_` names keeps working after `update.sh`, unedited.
- install.sh writes every key under both names, so a downgrade to v0.2.0 still connects; uninstall.sh removes both spellings and nothing else. `HERMES_BURROW_CHANNEL_REPO` is read before `HERMES_SUITE_CHANNEL_REPO`.
- The allowlist, allow-all and cron home-channel names Hermes reads itself are registered as the spelling the host sets (see README "Env names").
- `plugin.yaml` no longer declares `requires_env`, so `hermes plugins install` prompts for neither spelling; both are listed under `optional_env`.
- Display text says Burrow (platform label, plugin description, log lines, agent hint). Unchanged, because the desktop app and agents key on them: plugin name `startup-suite-platform`, platform `startup_suite`, MCP server `startup-suite`, the token file name, `.suite-channel/`, the installer result lines, `client_info.product` `hermes_channel`, and the `<suite-untrusted source="startup-suite">` envelope.

## v0.2.0

- `HERMES_SUITE_CHANNEL_REPO` env override: install.sh and update.sh can clone from a fork or mirror instead of the default repository.
- Org-neutral hints: install/update messages and the README no longer hard-code an org.

Also first tagged release of earlier unreleased work: `reply_to` forwarded as `in_reply_to_message_id` (#2), interpreter resolved through a `#!/bin/sh` launcher with `--python` (#3), and `SUITE_TRUSTED_USER_IDS` trusted principals (#5).
