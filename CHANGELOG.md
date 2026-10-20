# Changelog

## v0.4.1

- The MCP bridge (`hermes_suite_channel/mcp_bridge.py`) sends `User-Agent: burrow-channel-hermes/<version>` on every request. It used urllib's default `Python-urllib/3.x`, which Cloudflare in front of some Burrow hosts refuses with `403 error code: 1010`, so the agent's Burrow tools failed on those hosts.
- `hermes_suite_channel.__version__` and `plugin.yaml` `version:` are corrected: both still read `0.3.1` through v0.4.0. A test now pins them, and the bridge's User-Agent version, to `pyproject.toml`.

## v0.4.0

Straight rename of the identifiers that still carried the old product name. No alias and no dual-name support: this installer, `update.sh` and `uninstall.sh` read and write only the new names.

| What | Was | Now |
| --- | --- | --- |
| MCP server id (`config.yaml` `mcp_servers.<id>`, `MCP_NAME` in install.sh / uninstall.sh) | `startup-suite` | `burrow` |
| Plugin name (`plugin.yaml` `name:`, `PLUGIN_NAME` in install.sh / uninstall.sh / update.sh, the `$HERMES_HOME/plugins/<name>` directory, `plugins.enabled`) | `startup-suite-platform` | `burrow-platform` |
| Token file (`TOKEN_RELPATH` in install.sh / uninstall.sh, `TOKEN_FILE_RELPATH` in `hermes_suite_channel/config.py`) | `$HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token` | `$HERMES_HOME/mcp-tokens/burrow-platform.runtime-token` |
| Untrusted envelope `source=` attribute (`SOURCE` in `hermes_suite_channel/envelope.py`) | `source="startup-suite"` | `source="burrow"` |
| Hermes module namespace of the loaded plugin (derived by Hermes from the plugin name) | `hermes_plugins.startup_suite_platform` | `hermes_plugins.burrow_platform` |

- Tools reached through the MCP server are named by Hermes from the server id, so they move from the `startup_suite` prefix to the `burrow` prefix. Burrow's tool names themselves are unchanged.
- **Existing installs are renamed by burrow-cli, not by this installer.** `burrow hermes` (burrow-cli 0.12.0) moves an existing home's old MCP entry, plugin and token file to the new names on the agent's next START (never on an attach to a live session), then runs this installer. By hand, over an install made with an older version: this `update.sh` refuses (no `plugins/burrow-platform` yet, "run install.sh first"), and this `install.sh` does NOT remove the old names, so the home would end up with both generations (two MCP servers, two enabled platform plugins). Likewise this `uninstall.sh` removes only the new names. Use `burrow hermes` to move an existing home, or uninstall with the older version's `uninstall.sh` first.
- No `/new` is needed for the rename itself. The persisted platform hint (see v0.3.1) names neither `startup-suite` nor the plugin, so stored system prompts stay accurate. Earlier turns in a continuing session still show the old `source=` value and old tool-name prefix; new turns use the new ones. Restart the gateway to load this version.
- Deliberately unchanged: the gateway platform key `startup_suite` (`PLATFORM_NAME`, underscore form; it keys Hermes sessions and `platform_toolsets.startup_suite`, so renaming it would orphan session state, and it is not an MCP id), the Python package directory `hermes_suite_channel`, the installer result lines (`hermes-suite-channel: installed` / `updated` / `uninstalled`), the install record directory `$HERMES_HOME/.suite-channel/`, the `<suite-untrusted>` tag name, `client_info.product` `hermes_channel`, and LICENSE.

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
