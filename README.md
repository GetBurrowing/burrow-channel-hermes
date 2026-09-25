# hermes-suite-channel

Hermes Agent (Nous Research, https://github.com/NousResearch/hermes-agent)
platform plugin for Startup Suite federation. A sibling of
`claude-code-suite-channel` and `openclaw-suite-channel`: the same runtime
socket, runtime id and token, with Suite attention events delivered into a
Hermes session and replies pushed back.

Status: transport and Hermes platform adapter. The installer and the Suite
MCP registration land in stage 4 of task 01a0d6c5.

The repository root IS the Hermes plugin directory: `plugin.yaml`
(`kind: platform`, name `startup-suite-platform`), `__init__.py` exporting
`register`, and `adapter.py`. `register(ctx)` registers the gateway platform
`startup_suite` (config key `gateway.platforms.startup_suite`).

## Layout

| Path | What |
| --- | --- |
| `plugin.yaml`, `__init__.py`, `adapter.py` | the Hermes plugin entry points (root = plugin dir) |
| `hermes_suite_channel/adapter.py` | `SuiteAdapter(BasePlatformAdapter)` and `register(ctx)` |
| `hermes_suite_channel/inbound.py` | `parse_attention()`: one attention payload read once |
| `hermes_suite_channel/envelope.py` | `wrap_untrusted()`: the untrusted-content envelope |
| `hermes_suite_channel/suite_client.py` | asyncio Phoenix V2 client for core's `/runtime/ws` socket |
| `hermes_suite_channel/config.py` | `SuiteConfig.from_extra(extra, env)`: `PlatformConfig.extra` keys, env overrides |
| `hermes_suite_channel/redact.py` | `redact()` and `RedactingLogger`: the only path a log record takes |
| `hermes_suite_channel/_build.py` | `BUILD`, stamped by the installer, never read from git at runtime |
| `tests/fake_phoenix.py` | hermetic fake runtime socket on `websockets.serve` |

## Settings

Required: `url` (`SUITE_URL`), `runtime_id` (`SUITE_RUNTIME_ID`), and the
token, `SUITE_RUNTIME_TOKEN`. Env overrides `extra`.

The token is read from the environment ONLY (through Hermes's
`_shared.get_scoped_secret`, profile-scoped), never from `extra` and never
from the typed `PlatformConfig.token` field: Hermes serialises both in
`PlatformConfig.to_dict()`. `_env_enablement` seeds the url, runtime id and
tunables into `extra` and deliberately not the token.

Optional: `SUITE_ALLOWED_USERS`, `SUITE_ALLOW_ALL_USERS`, `SUITE_HOME_CHANNEL`
(cron delivery space), `SUITE_AGENT_ID` (see "Self-authored messages").

Tunables (an experiment surface; change the curve without a code change):

| `extra` key | env | default | source of the default |
| --- | --- | --- | --- |
| `reconnect_min_ms` | `SUITE_RECONNECT_MIN_MS` | 1000 | sibling `DEFAULT_RECONNECT_MS` |
| `reconnect_max_ms` | `SUITE_RECONNECT_MAX_MS` | 30000 | sibling `DEFAULT_MAX_RECONNECT_MS` |
| `reconnect_jitter` | `SUITE_RECONNECT_JITTER` | 0.2 | new; the sibling has no jitter |
| `stale_traffic_ms` | `SUITE_STALE_TRAFFIC_MS` | 120000 | sibling `STALE_TRAFFIC_MS` |
| `watchdog_interval_ms` | `SUITE_WATCHDOG_INTERVAL_MS` | 30000 | sibling `WATCHDOG_INTERVAL_MS` |
| `heartbeat_ms` | `SUITE_HEARTBEAT_MS` | 30000 | phoenix JS default heartbeat |
| `push_timeout_ms` | `SUITE_PUSH_TIMEOUT_MS` | 10000 | sibling `PUSH_TIMEOUT_MS` |
| `join_timeout_ms` | `SUITE_JOIN_TIMEOUT_MS` | 10000 | phoenix JS default join timeout |
| `dedupe_window_ms` | `SUITE_DEDUPE_WINDOW_MS` | 1000 | sibling's ~1s bucket |

## Inbound

An `attention` event becomes one Hermes `MessageEvent`:

| MessageEvent | from |
| --- | --- |
| `source.chat_id` | `signal.space_id`, else `signal.metadata.space_id` (a system_event names its space only there; reply target only) |
| `user_id` | `message.author_id`, else `message.author`; `suite-system` for a system_event |
| `user_name` | `message.author` |
| `message_id` | `signal.message_id` |
| `raw_message` | the payload |
| `text` | `wrap_untrusted(...)` around `message.content`, else `signal.prompt` |

`message.content` falling back to `signal.prompt` is the `system_event_prompt`
feature the handshake declares. A dispatch with neither gets
`System dispatch: <key> (<trigger>), no rendered prompt.`; an attention with
no body and nothing to correlate is dropped, as the Claude Code sibling does.
Correlation ids (`task_id`, `reason`, ...) are read from the signal root only;
core dual-writes them to the root and `metadata`. Events carry
`allow_gateway_control=False`: Suite text never resolves Hermes /commands.

Authorization. Hermes default-denies a platform with no allowlist. Suite core
already limits delivery to spaces the agent participates in, so the usual
setting is `SUITE_ALLOW_ALL_USERS=true`; `SUITE_ALLOWED_USERS` takes Suite
display names plus `suite-system` for task dispatches. Core sends no
`message.author_id` today, so allowlist entries are display names.

Self-authored messages are dropped: any attention whose `message_id` this
adapter posted, or whose `author_id` equals `signal.agent_id` or
`SUITE_AGENT_ID`. A `MessageDeduplicator` keyed on `message_id` (5 min TTL)
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

| Hermes adapter call | Suite frame |
| --- | --- |
| `send` (no `expect_edits`) | `reply`, acked; `SendResult.message_id` = Suite's id |
| `send_typing` / `stop_typing` | `typing` true / false |
| `send_document`, `send_image_file`, `send_video`, `send_voice`, `send_image` (`file://`, `data:`) | `reply_with_media`, base64 |
| `send_image` with an http(s) URL | a text `reply` with the link (not fetched: no fetch-anything proxy) |

Refusals come back as `SendResult` errors, never exceptions: `retryable` is
Suite's `recoverable`, true for not-connected, and false for a timeout or a
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
when the text is unchanged; `prefers_fresh_final_streaming` is False (Suite
cannot delete a preview); a finalized handle accepts the same text again as a
no-op and refuses different text. If the finalize `reply` fails, done:true is
not sent and the bubble stays with the last text. A stream Hermes abandons
without a finalize edit leaves its bubble until the viewer reloads.

Not built: history injection on first contact per space, and inbound
attachments (`message.attachments`) are not passed to Hermes yet.

## Token hygiene

The token lives only in `SuiteConfig.token` (excluded from `repr`) and the
socket query string. Every log record, including the `websockets` library's
DEBUG request line (which contains the query string), goes through
`RedactingLogger`. `tests/test_redaction.py` asserts a sentinel token reaches
no captured record across the connect, join-failure, reconnect and
push-timeout paths. A malformed `SUITE_URL` is terminal: `websockets`'
`InvalidURI` puts the whole URI, token included, in its message, so the client
re-raises it as `InvalidSuiteURL` with no URL and no chained original, stops
reconnecting, and the adapter's `connect()` fails with code `invalid_url`.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
git clone https://github.com/NousResearch/hermes-agent.git ../hermes-agent
git -C ../hermes-agent checkout fdec926ef54391edcf6caad5f7f6761fdcccdaa2
export HERMES_AGENT_SRC=$PWD/../hermes-agent
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

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
