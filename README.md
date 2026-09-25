# hermes-suite-channel

Hermes Agent (Nous Research, https://github.com/NousResearch/hermes-agent)
platform plugin for Startup Suite federation. A sibling of
`claude-code-suite-channel` and `openclaw-suite-channel`: the same runtime
socket, runtime id and token, with Suite attention events delivered into a
Hermes session and replies pushed back.

Status: transport only (`hermes_suite_channel/suite_client.py`). The Hermes
platform adapter (`plugin.yaml`, `adapter.py`) and the installer land in later
stages of task 01a0d6c5.

## Layout

| Path | What |
| --- | --- |
| `hermes_suite_channel/suite_client.py` | asyncio Phoenix V2 client for core's `/runtime/ws` socket |
| `hermes_suite_channel/config.py` | `SuiteConfig.from_extra(extra, env)`: `PlatformConfig.extra` keys, env overrides |
| `hermes_suite_channel/redact.py` | `redact()` and `RedactingLogger`: the only path a log record takes |
| `hermes_suite_channel/_build.py` | `BUILD`, stamped by the installer, never read from git at runtime |
| `tests/fake_phoenix.py` | hermetic fake runtime socket on `websockets.serve` |

## Settings

Required: `url` (`SUITE_URL`), `runtime_id` (`SUITE_RUNTIME_ID`),
`token` (`SUITE_TOKEN`). Env overrides `extra`.

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

## Token hygiene

The token lives only in `SuiteConfig.token` (excluded from `repr`) and the
socket query string. Every log record, including the `websockets` library's
DEBUG request line (which contains the query string), goes through
`RedactingLogger`. `tests/test_redaction.py` asserts a sentinel token reaches
no captured record across the connect, join-failure, reconnect and
push-timeout paths.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

The runtime dependency (`websockets`) is declared in `pyproject.toml`
`[project].dependencies`, which Hermes's package manager reads for directory
plugins. hermes-agent only pins `websockets` itself for Python >= 3.14.

## License

MIT, see [LICENSE](LICENSE). The text is copied from `openclaw-suite-channel`.
`claude-code-suite-channel` ships no LICENSE file (its `package.json` declares
MIT), so the openclaw copy is the only license text there is to match.
