# hermes-suite-channel

Hermes Agent (Nous Research, https://github.com/NousResearch/hermes-agent)
platform plugin for Burrow federation. A sibling of
`claude-code-suite-channel` and `openclaw-suite-channel`: the same runtime
socket, runtime id and token, with Burrow attention events delivered into a
Hermes session and replies pushed back.

Status: transport, Hermes platform adapter, installer (task 01a0d6c5 stages
1-4).

The repository root IS the Hermes plugin directory: `plugin.yaml`
(`kind: platform`, name `startup-suite-platform`), `__init__.py` exporting
`register`, and `adapter.py`. `register(ctx)` registers the gateway platform
`startup_suite` (config key `gateway.platforms.startup_suite`).

## Install

Prerequisite: Hermes Agent is installed and `hermes` is on PATH (or pass
`--hermes BIN`), and Hermes's runtime environment has the `mcp` Python
package (the `mcp` extra). Without it Hermes refuses the Burrow MCP server
with "requires the 'mcp' Python SDK, but it is not installed" (measured; the
socket channel still works, the Burrow tools do not). Note that once a plugin
with Python dependencies is enabled, Hermes runs on a per-home managed
environment (`$HERMES_HOME/installs/<id>/environments/<id>/venv`, measured at
fdec926e: it replaces the install's own site-packages on `sys.path`), so `mcp`
must be installed THERE; `mcp==2.0.0` is what was measured. In Burrow, register
a federated runtime for the agent and generate its token (the token is shown
once).

```sh
# From a checkout (the token on stdin; it is never accepted on argv):
printf '%s' "$BURROW_TOKEN" | ./install.sh \
  --url wss://suite.example.com/runtime/ws --runtime-id my-runtime --allow-all-users

# One command, no checkout (stdin is the script here, so the token comes from
# --token-file, else a no-echo prompt on the terminal):
curl -fsSL <raw url>/install.sh | bash -s -- \
  --url wss://suite.example.com/runtime/ws --runtime-id my-runtime \
  --token-file ~/.suite-token [--hermes-home DIR]

hermes gateway restart     # or `hermes gateway run` in the foreground
```

The token is read from `--token-file PATH` when given; else from stdin when
stdin is not a terminal and the script is a file (under `curl | bash` stdin is
the script); else from a no-echo prompt on `/dev/tty`. `--token` and
`--token=...` are refused with an error: argv is readable by every local user
through `ps` and `/proc/<pid>/cmdline`.

What install.sh changes, and nothing else:

| Where | What |
| --- | --- |
| `$HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token` (mode 0600, directory 0700) | the token, and nothing else. Never in `.env`: see Security notes |
| `$HERMES_HOME/.env` (mode 0600) | `BURROW_URL`, `BURROW_RUNTIME_ID`, `BURROW_RUNTIME_TOKEN_FILE` (the path above); `BURROW_ALLOW_ALL_USERS=true` with `--allow-all-users`, `BURROW_ALLOWED_USERS` with `--allowed-users CSV`. Each is ALSO written under its deprecated `SUITE_` name, so a downgrade to v0.2.0 (which reads only `SUITE_`) still connects. A `BURROW_RUNTIME_TOKEN` or `SUITE_RUNTIME_TOKEN` line an older install wrote is removed |
| `$HERMES_HOME/plugins/startup-suite-platform` | the plugin, via `hermes plugins install <source> --ref <sha> --no-enable`, then `hermes plugins enable startup-suite-platform` |
| `hermes_suite_channel/_build.py` in that copy | `BUILD = "<installed sha>"` (the build stamp core shows as the runtime's build) |
| `config.yaml` `mcp_servers.startup-suite` | via `hermes config set`: a stdio server, `command` + `args` (see Burrow tools). The `url` / `headers` form an older install wrote is unset |
| `$HERMES_HOME/.suite-channel/` (0700) | the install record, written once by the FIRST install: a 0600 copy of `config.yaml` as it was, and which of `config.yaml`, `.env`, `mcp-tokens/` did not exist yet. uninstall.sh uses it and then deletes it |

`$HERMES_HOME` defaults to `$HERMES_HOME`, else `~/.hermes`. `.env` is Hermes's
secret store (`hermes_cli/config.py` `get_env_path`); install.sh writes the
lines itself, in the unquoted `KEY=value` form Hermes's own `save_env_value`
writes, and refuses a value with whitespace, `#`, a quote or a backslash so
that stays true. The env keys are written before the plugin is installed:
v0.2.0's manifest has `requires_env`, and `hermes plugins install` prompts
for any unset one.

Two install details that were measured, not assumed (hermes-agent fdec926e):

- **Enable is a separate step.** A non-interactive `hermes plugins install
  --enable` refuses to enable a plugin that declares Python dependencies
  ("dependency install skipped (non-interactive)"). `hermes plugins enable`
  run non-interactively does resolve and install them, so install.sh installs
  with `--no-enable` and then enables.
- **Private repository.** `hermes plugins install` accepts `file://` sources
  (with a warning) and `--ref <40-char sha>`. Run from a checkout, install.sh
  installs `file://<checkout>` at its HEAD. Without a checkout it clones
  `--repo URL`, else `$HERMES_BURROW_CHANNEL_REPO`, else this repository's
  GitHub URL, with your own git credentials (update.sh resolves the same way
  and passes the result on), asks Hermes to install the https URL (Hermes
  tries `GITHUB_TOKEN`/`GH_TOKEN` from `.env`, then `gh auth`, then the git
  credential helper), and retries from the local clone. `--copy-fallback`
  copies the committed tree into `$HERMES_HOME/plugins/` when Hermes refuses
  both. It is opt-in because the exit code cannot tell a fetch failure from a
  refusal by Hermes's install-time security scan, and a copied plugin has no
  Hermes install record.

- **Install-time scan.** Hermes scans the plugin tree before installing it
  (tools/plugin_guard.py): `safe` installs, `caution` asks for confirmation
  and is refused non-interactively, `dangerous` is refused even with
  `--force`. This tree scans `caution`: the HIGH findings are this README's own
  `curl | bash` install lines (measured on 37cafc2: 3 HIGH, 22 MEDIUM, 0
  CRITICAL). install.sh therefore retries a `caution` refusal once with
  `--force` and says so on stderr; `--refuse-scan-caution` makes it stop
  instead. It never retries a `dangerous` verdict.

Every run is idempotent: a second run with the same arguments changes no file
(the plugin at the same sha is not reinstalled, the token file is compared
with `cmp` against a fresh temp copy rather than read into the shell, and
`config set` is skipped when the value already matches). The match is decided
on the RAW YAML (parsed with ruamel.yaml on Hermes's own interpreter: the
one `--python PATH` names, else the launcher's shebang when that is a Python,
else the dependency environment Hermes itself reports through `hermes
--run-module pm.environments`, else python3 on PATH; at hermes-agent fdec926e
every launcher is a `#!/bin/sh` wrapper, so the shebang is never the answer
there), never through `hermes config get
--raw`, which expands `${VAR}` placeholders from the environment and would
hand an expanded value to the shell through a child's stdout. Measured
against the real CLI (fdec926e): the second install prints no `Set` line and
leaves `config.yaml`, `.env` and the token file byte-identical. The last line printed on success is exactly
`hermes-suite-channel: installed`; uninstall.sh and update.sh end with
`hermes-suite-channel: uninstalled` and `hermes-suite-channel: updated`. The
Burrow desktop app keys on those lines.

Set `BURROW_HOME_CHANNEL` (a Burrow space id) if you want cron output
delivered somewhere specific. Without it Hermes posts its own "No home channel
is set" notice into the first Burrow space it answers in (seen in the live
proof); that text comes from Hermes, not from this plugin.

### Uninstall

```sh
./uninstall.sh [--hermes-home DIR]
```

Removes, in order: `mcp_servers.startup-suite` (`hermes config unset`), the
plugin (`hermes plugins remove`; a copy-fallback install is disabled and
deleted), the config.yaml bookkeeping below, the token file (and
`mcp-tokens/` if the install created it and it is empty), the install
record, and the `.env` lines defining `BURROW_URL`, `BURROW_RUNTIME_ID`,
`BURROW_RUNTIME_TOKEN_FILE`, `BURROW_RUNTIME_TOKEN`, `BURROW_ALLOW_ALL_USERS` and
`BURROW_ALLOWED_USERS`, and the same six under their `SUITE_` names. Other `.env` lines are kept byte for byte, and a `.env`
the install created is deleted when nothing else is left in it.

**config.yaml.** Hermes's own writer adds keys nobody asked it to: `hermes
plugins enable` writes `plugins: {enabled: [...], disabled: []}` and `hermes
plugins remove` leaves `enabled: []` behind and stamps `_config_version: 46`
(measured, fdec926e). When the install record shows those keys were absent
before the first install, uninstall.sh takes them away again:

- if `config.yaml` minus those keys equals the saved pre-install copy, the
  saved bytes are written back (comments and layout included), so the file's
  sha256 is the original one;
- otherwise the file was edited after the install (by you or by Hermes), and
  only those top-level keys are removed, in their empty shape; it says so on
  stderr. Unrelated keys are never touched.

Measured with the real CLI (fdec926e), a `config.yaml` holding a commented
`model:` block: install, reinstall, uninstall gives back the original sha256
(`tests/test_install_scripts.py::test_round_trip_with_the_real_hermes_cli`,
run with `HERMES_REAL_BIN`). After a GATEWAY RUN it cannot: the gateway
itself writes `onboarding.seen.profile_build_offered: true`, so the live rig
took the second branch and ended with the original keys plus that one
Hermes-written key. Two more things are Hermes's, not this plugin's, and are
left alone: `$HERMES_HOME/backups/config/config.yaml.good.*`, the snapshots
`hermes config set` writes (3 after the first install and 4 after the second,
measured), and a missing final newline on `.env` (install.sh terminates the
last line before appending, as Hermes's writer does). An install over an
older version that left no record keeps `config.yaml` as it is apart from the
MCP entry, and says so.

The tests' stub `hermes` writes real YAML (ruamel round-trip) and mimics the
bookkeeping above, so the stub round-trip test exercises the cleanup;
`test_bookkeeping_check_fires_without_the_cleanup` runs an uninstaller with
that step cut out and requires the sha comparison to fail.

### Update

```sh
./update.sh [--hermes-home DIR]            # install this checkout's HEAD
curl -fsSL <raw url>/update.sh | bash -s -- [--ref SHA]
```

Like `openclaw-suite-channel`'s update.sh: it refreshes the plugin files and
preserves the configuration. `.env` and the MCP entry are not touched and the
token is not read. It runs `install.sh --plugin-only`, which reinstalls with
`--force --ref <sha>` and restamps `_build.py`. Restart the gateway afterwards.
Because it leaves the configuration alone, it does NOT move an older install's
token out of `.env`; re-run install.sh for that.

## Burrow tools (MCP)

Burrow's tools (messaging, tasks, plans, canvases, memory, ...) are served by
core's `/mcp` endpoint under the same runtime token (ADR 0034). This plugin
registers no tools of its own. install.sh registers the endpoint as a Hermes
**stdio** MCP server that relays to it:

```yaml
mcp_servers:
  startup-suite:
    command: /path/to/hermes/venv/bin/python   # the interpreter Hermes's own script names
    args:
      - $HERMES_HOME/plugins/startup-suite-platform/hermes_suite_channel/mcp_bridge.py
      - --url
      - https://suite.example.com/mcp          # derived from --url; --mcp-url overrides
      - --token-file
      - $HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token
```

Why not the plain HTTP form (`url` + `headers: {Authorization: Bearer
${BURROW_RUNTIME_TOKEN}}`), which the first version used: Hermes resolves an
HTTP server's headers ONLY by `${VAR}` interpolation from the environment
(tools/mcp_tool_config.py `_interpolate_env_vars`; the only other resolvers
are fixed `${userHome}`-style context names), and its own `hermes mcp add
--auth header` stores the token in `.env` for the same interpolation
(hermes_cli/mcp_config.py `_bearer_auth_headers`). Neither a file- or
command-backed header, nor a plugin hook that sets a server's headers in
memory, exists at fdec926e (checked: `PluginContext` has no MCP-server
registrar; portable Agent Plugins' `server_json` liveness is for local apps).
The HTTP form therefore needs the token in the gateway's `os.environ`, and see
Security notes for what that costs.

`hermes_suite_channel/mcp_bridge.py` is standard library only: one JSON-RPC
message per stdin line is POSTed to `--url` with the token in the
`Authorization` header; a JSON or `text/event-stream` reply is written back
one message per line, 202/204 writes nothing, and `Mcp-Session-Id` /
`MCP-Protocol-Version` are carried forward. It reads the token from the file
on every request (a rotated token needs no restart), refuses a token file
group or other can read, and phrases every failure without it. Hermes spawns
a stdio server with a filtered environment (`_build_safe_env`), and the
bridge needs nothing from it. Burrow's endpoint sends no server-initiated
messages (the GET stream answers 405), so none are relayed. Measured on the
live rig: `hermes mcp test startup-suite` connects and discovers 24 tools,
and an agent turn called `mcp__startup_suite__space_list` through it.

## Layout

| Path | What |
| --- | --- |
| `plugin.yaml`, `__init__.py`, `adapter.py` | the Hermes plugin entry points (root = plugin dir) |
| `hermes_suite_channel/adapter.py` | `SuiteAdapter(BasePlatformAdapter)` and `register(ctx)` |
| `hermes_suite_channel/inbound.py` | `parse_attention()`: one attention payload read once |
| `hermes_suite_channel/envelope.py` | `wrap_untrusted()`: the untrusted-content envelope; `frame_trusted()`: a trusted owner's framing |
| `hermes_suite_channel/suite_client.py` | asyncio Phoenix V2 client for core's `/runtime/ws` socket |
| `hermes_suite_channel/config.py` | `SuiteConfig.from_extra(extra, env)`: `PlatformConfig.extra` keys, env overrides; `resolve_token()`, the token file |
| `hermes_suite_channel/mcp_bridge.py` | the stdio MCP server that relays to Burrow's `/mcp` with the token from the file |
| `hermes_suite_channel/redact.py` | `redact()` and `RedactingLogger`: the only path a log record takes |
| `hermes_suite_channel/_build.py` | `BUILD`, stamped by the installer, never read from git at runtime |
| `tests/fake_phoenix.py` | hermetic fake runtime socket on `websockets.serve` |
| `install.sh`, `uninstall.sh`, `update.sh` | the installer (see Install) |
| `tests/stub_hermes/hermes`, `tests/test_install_scripts.py` | the installer tests, against a stub `hermes` that records its argv (plus one against the real CLI when `HERMES_REAL_BIN` is set) |
| `tests/test_mcp_bridge.py` | the bridge as a subprocess against an HTTP endpoint shaped like Burrow's |

## Settings

### Env names: `BURROW_*` first, `SUITE_*` deprecated

Every setting below is read as `BURROW_<X>` first and, when that is unset or
blank, as the deprecated `SUITE_<X>`; the first use of an old name logs one
warning per name per process (`Burrow: SUITE_URL is deprecated; set
BURROW_URL`). A host installed by v0.2.0, whose `.env` holds only `SUITE_`
names, keeps working after `update.sh` with no edit. The full list is
`ENV_FALLBACKS` in `hermes_suite_channel/config.py` (pinned by a test); the
installer's repository override follows the same rule
(`HERMES_BURROW_CHANNEL_REPO`, then `HERMES_SUITE_CHANNEL_REPO`).

Hermes reads three of them itself, by ONE name the plugin registers at load:
the allowlist (`*_ALLOWED_USERS`), allow-all (`*_ALLOW_ALL_USERS`) and the cron
home channel (`*_HOME_CHANNEL`, and Hermes's own `<that name>_NAME`). Measured
at hermes-agent fdec926e (`gateway/authz_mixin.py` `_principal_authorized`):
registering `BURROW_ALLOWED_USERS` on a host that sets only
`SUITE_ALLOWED_USERS=Ryan` denies Ryan as well as everyone else, and
`SUITE_ALLOW_ALL_USERS=true` stops admitting anyone. So `register()` passes the
spelling the host sets (`BURROW_` when set, else `SUITE_`, else `BURROW_`).
Two consequences:

- Rename one of those three in `.env` and restart the gateway; the per-turn
  `.env` reload picks up a new VALUE, not a new name.
- Under multiplexing, the launch profile's spelling is the one registered for
  every profile. A secondary profile that spells it differently has its
  allowlist ignored, which denies (Hermes's default), never admits; use one
  spelling across profiles.

`required_env` (what `hermes setup` lists and what `hermes profile create
--clone` strips) declares every spelling the host sets, so a clone of a host
install.sh wrote loses both `BURROW_URL` and `SUITE_URL`. `plugin.yaml`
declares no `requires_env`: Hermes prompts at `hermes plugins install` (which
update.sh runs) for each one unset in `.env`, one spelling per entry, so
either choice would prompt one kind of host. Both spellings are listed under
`optional_env` for `hermes config`.

Required: `url` (`BURROW_URL`), `runtime_id` (`BURROW_RUNTIME_ID`), and the
token. Env overrides `extra`.

The token comes from, in order: the file `BURROW_RUNTIME_TOKEN_FILE` names;
`$HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token` (per profile,
through `get_hermes_home()`); the legacy `BURROW_RUNTIME_TOKEN` secret; and
last the value `register()` took out of `os.environ` at plugin load. Every
name is read through Hermes's `_shared.get_scoped_secret` (profile-scoped),
and the last rung is never used inside a multiplexed secondary profile's
scope, because it is the launch profile's value. A token file group or other
can read is refused (`chmod 600`), naming the path, never the content. The
token is never read from `extra` or the typed `PlatformConfig.token` field:
Hermes serialises both in `PlatformConfig.to_dict()`. `_env_enablement`
seeds the url, runtime id and tunables into `extra` and deliberately not the
token.

Optional: `BURROW_ALLOWED_USERS`, `BURROW_ALLOW_ALL_USERS`, `BURROW_HOME_CHANNEL`
(cron delivery space), `BURROW_AGENT_ID` (see "Self-authored messages"),
`BURROW_TRUSTED_USER_IDS` (see "Trusted principals"; `extra` key
`trusted_user_ids`, a comma string or a list).

Tunables (an experiment surface; change the curve without a code change):

| `extra` key | env | default | source of the default |
| --- | --- | --- | --- |
| `reconnect_min_ms` | `BURROW_RECONNECT_MIN_MS` | 1000 | sibling `DEFAULT_RECONNECT_MS` |
| `reconnect_max_ms` | `BURROW_RECONNECT_MAX_MS` | 30000 | sibling `DEFAULT_MAX_RECONNECT_MS` |
| `reconnect_jitter` | `BURROW_RECONNECT_JITTER` | 0.2 | new; the sibling has no jitter |
| `stale_traffic_ms` | `BURROW_STALE_TRAFFIC_MS` | 120000 | sibling `STALE_TRAFFIC_MS` |
| `watchdog_interval_ms` | `BURROW_WATCHDOG_INTERVAL_MS` | 30000 | sibling `WATCHDOG_INTERVAL_MS` |
| `heartbeat_ms` | `BURROW_HEARTBEAT_MS` | 30000 | phoenix JS default heartbeat |
| `push_timeout_ms` | `BURROW_PUSH_TIMEOUT_MS` | 10000 | sibling `PUSH_TIMEOUT_MS` |
| `join_timeout_ms` | `BURROW_JOIN_TIMEOUT_MS` | 10000 | phoenix JS default join timeout |
| `dedupe_window_ms` | `BURROW_DEDUPE_WINDOW_MS` | 1000 | sibling's ~1s bucket |

## Inbound

An `attention` event becomes one Hermes `MessageEvent`:

| MessageEvent | from |
| --- | --- |
| `source.chat_id` | `signal.space_id`, else `signal.metadata.space_id` (a system_event names its space only there; reply target only) |
| `user_id` | `message.author_id`, else `message.author`; `suite-system` for a system_event |
| `user_name` | `message.author` |
| `message_id` | `signal.message_id` |
| `raw_message` | the payload |
| `text` | `wrap_untrusted(...)` around `message.content`, else `signal.prompt`; `frame_trusted(...)` instead for a trusted principal |
| `metadata["suite_trusted_author"]` | `True` only when the message was delivered as a trusted principal's |

`message.content` falling back to `signal.prompt` is the `system_event_prompt`
feature the handshake declares. A dispatch with neither gets
`System dispatch: <key> (<trigger>), no rendered prompt.`; an attention with
no body and nothing to correlate is dropped, as the Claude Code sibling does.
Correlation ids (`task_id`, `reason`, ...) are read from the signal root only;
core dual-writes them to the root and `metadata`. Events carry
`allow_gateway_control=False`: Burrow text never resolves Hermes /commands.

Authorization. Hermes default-denies a platform with no allowlist. Burrow core
already limits delivery to spaces the agent participates in, so the usual
setting is `BURROW_ALLOW_ALL_USERS=true`; `BURROW_ALLOWED_USERS` takes Burrow
display names plus `suite-system` for task dispatches. That allowlist decides
who may START a turn, and it is keyed on display names, so it grants no trust:
whether a message is an instruction is decided separately, by
`BURROW_TRUSTED_USER_IDS` on a server-set user id (below).

Author identity. Since core PR #1169 a chat attention's `message` carries
`author_type` (`user` / `agent` / `system`, the authoring participant's type),
`author_user_id` (a user's Burrow user id; `null` for anyone else) and
`author_agent_id` (an agent's id; `null` for anyone else). Core reads them
from the participant row, not from the message's caller-settable author
snapshot. There is still no `message.author_id`. A system_event and a
TaskRouter dispatch carry no user author.

### Trusted principals

`BURROW_TRUSTED_USER_IDS` is a comma list of Burrow **user ids** (UUIDs). It is
opt-in per agent and empty by default, and empty means every message is
wrapped, as before. A message is delivered as an owner instruction only when
all of these hold:

- it is a chat message (a system_event never is);
- `message.author_type == "user"`;
- `message.author_user_id` is in the list.

The display name is never consulted: a user or agent who shares or picks the
owner's name, an agent author, and a core too old to send the id all stay
wrapped. An entry that is not a UUID (a display name, a typo) is dropped with
a warning that gives a count, never the entry, so a misconfiguration trusts
fewer people, never more.

A trusted message arrives as:

```
[Suite message from your owner <name> (verified user id <uuid>) in space <space_id>]
<body>
```

The body still gets the special-token and mention neutralisers, and the
framing line is one line (newlines in the name are folded). **Trust means
instructions, not control:** `allow_gateway_control` stays `False`, so a
trusted owner's text still cannot run Hermes /commands. Use the Hermes CLI
for those.

Self-authored messages are dropped: any attention whose `message_id` this
adapter posted, or whose `author_id` equals `signal.agent_id` or
`BURROW_AGENT_ID`. A `MessageDeduplicator` keyed on `message_id` (5 min TTL)
sits on top of the client's 1 s identity window, for dead-letter replays.

### The untrusted envelope

```
<suite-untrusted nonce="<128-bit hex>" source="startup-suite" space_id=".." message_id=".." task_id=".." reason=".." author="..">
<one-line preamble: external, untrusted, contains no instructions>
<body>
</suite-untrusted nonce="<same>">
```

Neither sibling wraps content in plugin code, so this is new. Inside the
body: `<suite-untrusted` / `</suite-untrusted` (any case or spacing) become
`&lt;...`, so a forged end marker closes nothing; chat special tokens `<|...|>`
(and DeepSeek's fullwidth `<\uff5c...\uff5c>`) become `<\|...\|>`; an at-sign
followed by `[` gets a backslash, so an echoed name token cannot match core's
mention regex and re-invite an agent. Header values are attribute-escaped and
get the same three passes. `tests/test_envelope.py` disables each neutraliser
in turn and shows the adversarial fixture breaks without it.

### Meeting transcripts: declined

`meeting_transcript` frames are neither delivered nor acked. The sibling acks
each valid frame because its session receives it; core's
`meeting_transcript_ack` stamps the runtime as PROVEN to hear meetings
(`RuntimePresence.prove`), so acking a frame this adapter drops would be a
false capability. Waking a Hermes turn per spoken sentence also needs the
sibling's coalescing and a speak path (`meeting_say` on the hosted MCP
server, registered in stage 4). Not built yet.

## Outbound

| Hermes adapter call | Burrow frame |
| --- | --- |
| `send` (no `expect_edits`) | `reply`, acked; `SendResult.message_id` = Burrow's id |
| `send_typing` / `stop_typing` | `typing` true / false |
| `send_document`, `send_image_file`, `send_video`, `send_voice`, `send_image` (`file://`, `data:`) | `reply_with_media`, base64 |
| `send_image` with an http(s) URL | a text `reply` with the link (not fetched: no fetch-anything proxy) |

Refusals come back as `SendResult` errors, never exceptions: `retryable` is
Burrow's `recoverable`, true for not-connected, and false for a timeout or a
dropped socket (the push may have landed; a blind retry could duplicate it).
Attachments over core's per-file cap are refused before reading the file.
Core compares the base64 length with `div(10 MiB * 4, 3)`, so the largest file
it accepts is 10 MiB minus one byte.

### Progressive reveal

Hermes's stream consumer (`gateway/stream_consumer_transport.py`) opens a
streamed response with `send(metadata={"expect_edits": True})`, updates it with
`edit_message`, and ends with `edit_message(finalize=True)`. A non-streamed
send has no `expect_edits`. So:

1. `send` with `expect_edits` -> new `chunk_id`, `reply_chunk` done:false,
   returns handle `chunk:<id>`;
2. each `edit_message` -> cumulative `reply_chunk` done:false, same `chunk_id`;
3. `edit_message(finalize=True)` -> an acked `reply` with the final text, THEN
   `reply_chunk` done:true.

Step 3 persists. Core's `reply_chunk` is an ephemeral broadcast with no row,
and the chat view deletes the streaming bubble on done:true, so done:true
alone would lose the answer; the Claude Code sibling also posts a `reply` on
its last chunk. `REQUIRES_EDIT_FINALIZE` makes the finalize edit arrive even
when the text is unchanged; `prefers_fresh_final_streaming` is False (Burrow
cannot delete a preview); a finalized handle accepts the same text again as a
no-op and refuses different text. If the finalize `reply` fails, done:true is
not sent and the bubble stays with the last text. A stream Hermes abandons
without a finalize edit leaves its bubble until the viewer reloads.

Not built: history injection on first contact per space, and inbound
attachments (`message.attachments`) are not passed to Hermes yet.

## Token hygiene

In the gateway the token lives only in `SuiteConfig.token` (excluded from
`repr`) and the socket query string; in the MCP bridge, only in the
`Authorization` header of each request. Both read it from the token file. Every log record, including the `websockets` library's
DEBUG request line (which contains the query string), goes through
`RedactingLogger`. `tests/test_redaction.py` asserts a sentinel token reaches
no captured record across the connect, join-failure, reconnect and
push-timeout paths. A malformed `BURROW_URL` is terminal: `websockets`'
`InvalidURI` puts the whole URI, token included, in its message, so the client
re-raises it as `InvalidSuiteURL` with no URL and no chained original, stops
reconnecting, and the adapter's `connect()` fails with code `invalid_url`.

## Security notes

- The token is never on any process's argv. install.sh reads and writes it
  with bash builtins only (`read`, `printf`), and every `hermes` child runs
  with stdin on `/dev/null`. `tests/test_install_scripts.py` asserts a
  sentinel token is absent from the argv of every `hermes` call (the stub
  records them) and, where `strace` exists, from every `execve` of the whole
  install; a companion test runs an installer mutated to pass the token to
  `hermes config set` and requires the same check to catch it.
- **The token is in no process environment.** It is written only to
  `$HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token` (0600, in
  the directory Hermes's file tools, media delivery, context references and
  dashboard file router all refuse to read: agent/file_safety.py
  `_READ_DENIED_DIRS`, gateway/platforms/base.py), not to `.env`, not to
  `config.yaml`, and the adapter never puts it in `PlatformConfig` (see Token
  hygiene).
- **Why not `.env`: every child Hermes spawns without a scrub inherits it.**
  Hermes copies `$HERMES_HOME/.env` into the gateway's `os.environ` at start,
  again on EVERY gateway turn (gateway/run.py
  `_reload_runtime_env_preserving_config_authority`, non-multiplexed) and
  before every MCP config load (tools/mcp_tool_config.py `_load_mcp_config`).
  Several spawn paths pass that environment on unfiltered; the one the review
  found is the toolchain probe (tools/env_probe.py `_run`: a bare
  `subprocess.run`, so `pip --version` and `python3 -c ...` get everything).
  `BURROW_RUNTIME_TOKEN` is not in Hermes's `_ALWAYS_STRIP_KEYS`, and env_probe
  does not apply that set anyway; the terminal tool does scrub. Removing the
  variable from `os.environ` at plugin load (which `register()` still does,
  as a second layer for a launcher-exported token) does not survive the next
  reload. Measured on a live gateway (fdec926e), tracing every `execve` with
  its environment (`strace -f -v -e trace=execve`) and a canary variable
  exported to the gateway as the positive control:

  | install | execs traced | carrying the canary | carrying the token |
  | --- | --- | --- | --- |
  | this version (token file) | 132 over 209 s, incl. `pip`, `git`, the terminal tool's `bash` | 131 (all but the MCP bridge, whose env Hermes filters) | 0 |
  | control: the same, plus the legacy `SUITE_RUNTIME_TOKEN` line in `.env` | 114 over 92 s | 113 | 76 (`pip`, `python3`, `git`, `bash`, ...) despite the pop at load |

  A 0.1 s `/proc/<pid>/environ` poll of the gateway's descendants for 240 s
  (the review's method) also read 0 token and 49 canary hits. It did not see
  the short-lived probe children at all, which is why the trace is the
  measurement above.
- **Upstream Hermes defect, not fixed here.** Any secret in `.env` reaches
  every child spawned without a scrub, for every plugin and provider key, not
  only this one. Nothing is filed upstream from this repository. There is
  also no plugin hook that adds a key to the strip set for ALL spawn paths:
  `TerminalEnvironmentProvider.strip_env_keys`
  (agent/terminal_env_registry.py) feeds the Tier-1 scrub of the paths that
  scrub, and env_probe is not one of them.
- `plugin.yaml` requires nothing (see "Env names"), so `hermes plugins
  install` prompts for nothing; it lists the legacy `BURROW_RUNTIME_TOKEN`
  and `SUITE_RUNTIME_TOKEN` as optional with `secret: true` and
  `password: true`: Hermes's install-time prompt masks only `secret`
  entries, `password` masks it in `hermes config`.
  `tests/test_plugin_load.py` drives Hermes's own prompt function.
- Burrow message text arrives wrapped in the untrusted envelope, except a
  message whose server-set author is a user listed in
  `BURROW_TRUSTED_USER_IDS` (empty by default). No message, trusted or not,
  can run Hermes slash commands (`allow_gateway_control=False`).
- Revoking the runtime in Burrow (or `Platform.Federation.revoke_runtime/1`)
  clears its token hash; the old token is then refused on reconnect.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
git clone https://github.com/NousResearch/hermes-agent.git ../hermes-agent
git -C ../hermes-agent checkout fdec926ef54391edcf6caad5f7f6761fdcccdaa2
export HERMES_AGENT_SRC=$PWD/../hermes-agent
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

With `HERMES_REAL_BIN=/path/to/hermes` set, one more installer test runs the
round trip against the real CLI in a temp `HERMES_HOME`; without it that test
reports as skipped.

The adapter tests run against the REAL hermes-agent gateway (base adapter,
`MessageEvent`, `PlatformEntry`, the plugin loader, and the stream consumer),
not a stub. Without `HERMES_AGENT_SRC` they fail at collection rather than
skip. CI checks out the same commit.

The runtime dependency (`websockets`) is declared in `pyproject.toml`
`[project].dependencies`, which Hermes's package manager reads for directory
plugins. hermes-agent only pins `websockets` itself for Python >= 3.14.

## License

MIT, see [LICENSE](LICENSE). The text is copied from `openclaw-suite-channel`.
`claude-code-suite-channel` ships no LICENSE file (its `package.json` declares
MIT), so the openclaw copy is the only license text there is to match.
