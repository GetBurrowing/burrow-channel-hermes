#!/usr/bin/env bash
# Remove what install.sh added to a Hermes Agent home, and nothing else:
#   - config.yaml `mcp_servers.startup-suite` (through `hermes config unset`);
#   - the plugin `startup-suite-platform` (through `hermes plugins remove`);
#   - the .env lines defining SUITE_URL, SUITE_RUNTIME_ID, SUITE_RUNTIME_TOKEN,
#     SUITE_ALLOW_ALL_USERS and SUITE_ALLOWED_USERS. Every other .env line is
#     kept byte for byte.
#
#   ./uninstall.sh [--hermes-home DIR] [--hermes BIN]
#   curl -fsSL <raw url>/uninstall.sh | bash -s -- [--hermes-home DIR]
#
# Safe to run twice: the second run finds nothing and changes nothing.
# The last line printed on success is exactly `hermes-suite-channel: uninstalled`.

set -euo pipefail

readonly PLUGIN_NAME="startup-suite-platform"
readonly MCP_NAME="startup-suite"
readonly ENV_KEYS=(SUITE_URL SUITE_RUNTIME_ID SUITE_RUNTIME_TOKEN SUITE_ALLOW_ALL_USERS SUITE_ALLOWED_USERS)
readonly RESULT_LINE="hermes-suite-channel: uninstalled"

log() { printf 'hermes-suite-channel: %s\n' "$*" >&2; }
die() {
  printf 'hermes-suite-channel: error: %s\n' "$*" >&2
  exit 1
}

HERMES_HOME_ARG=""
HERMES_BIN="${HERMES_BIN:-hermes}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --hermes-home) [ -n "${2:-}" ] || die "--hermes-home needs a value"; HERMES_HOME_ARG="$2"; shift 2 ;;
    --hermes-home=*) HERMES_HOME_ARG="${1#*=}"; shift ;;
    --hermes) [ -n "${2:-}" ] || die "--hermes needs a value"; HERMES_BIN="$2"; shift 2 ;;
    --hermes=*) HERMES_BIN="${1#*=}"; shift ;;
    -h | --help)
      echo "usage: uninstall.sh [--hermes-home DIR] [--hermes BIN]" >&2
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

HERMES_HOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
export HERMES_HOME
[ -d "$HERMES_HOME" ] || die "no Hermes home at $HERMES_HOME"
command -v "$HERMES_BIN" >/dev/null 2>&1 || die "hermes not found (looked for '$HERMES_BIN'); pass --hermes BIN"

run_hermes() { "$HERMES_BIN" "$@" </dev/null; }

# 1. The MCP entry. Removed first: the header references SUITE_RUNTIME_TOKEN.
if run_hermes config get "mcp_servers.$MCP_NAME.url" --raw >/dev/null 2>&1; then
  run_hermes config unset "mcp_servers.$MCP_NAME" >&2 || die "hermes config unset mcp_servers.$MCP_NAME failed"
  log "removed MCP server $MCP_NAME"
fi

# 2. The plugin.
PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_NAME"
if [ -e "$PLUGIN_DIR" ]; then
  if ! run_hermes plugins remove "$PLUGIN_NAME" >&2; then
    # A --copy-fallback install has no Hermes install metadata.
    run_hermes plugins disable "$PLUGIN_NAME" >&2 || true
    rm -rf "$PLUGIN_DIR"
  fi
  [ ! -e "$PLUGIN_DIR" ] || die "plugin directory $PLUGIN_DIR is still present"
  log "removed plugin $PLUGIN_NAME"
fi

# 3. The .env keys. Bash builtins only; lines are compared by key, so no value
# (the token included) is ever passed to a child process.
ENV_FILE="$HERMES_HOME/.env"
if [ -f "$ENV_FILE" ]; then
  keys_re="$(
    IFS='|'
    printf '%s' "${ENV_KEYS[*]}"
  )"
  re="^[[:space:]]*(export[[:space:]]+)?(${keys_re})[[:space:]]*="
  out=""
  removed=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ $re ]]; then
      removed=1
    else
      out+="$line"$'\n'
    fi
  done <"$ENV_FILE"
  if [ "$removed" = 1 ]; then
    tmp="$(umask 077 && mktemp "$HERMES_HOME/.env_hsc.XXXXXX")"
    printf '%s' "$out" >"$tmp"
    chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
    log "removed the Suite keys from $ENV_FILE"
  fi
fi

log "restart the gateway if it is running: hermes gateway restart"
printf '%s\n' "$RESULT_LINE"
