#!/usr/bin/env bash
# Install the Startup Suite channel plugin into a Hermes Agent home.
#
#   curl -fsSL <raw url>/install.sh | bash -s -- \
#     --url wss://<host>/runtime/ws --runtime-id <id> [--hermes-home DIR] \
#     [--token-file PATH]
#
#   printf '%s' "$TOKEN" | ./install.sh --url ... --runtime-id ...   # from a checkout
#
# The runtime token is NEVER accepted on the command line (argv is readable by
# every local user through ps and /proc). It is read, in order, from:
#   1. --token-file PATH, when given;
#   2. stdin, when stdin is not a terminal AND this script is not itself being
#      read from stdin (under `curl | bash` stdin is the script, not a token);
#   3. a no-echo prompt on the controlling terminal.
#
# What it changes, and nothing else (uninstall.sh reverses exactly this):
#   - $HERMES_HOME/.env: SUITE_URL, SUITE_RUNTIME_ID, SUITE_RUNTIME_TOKEN
#     (plus SUITE_ALLOW_ALL_USERS / SUITE_ALLOWED_USERS when asked); mode 0600.
#   - the plugin `startup-suite-platform`, installed and enabled through
#     `hermes plugins install` + `hermes plugins enable`;
#   - config.yaml `mcp_servers.startup-suite`: url https://<host>/mcp and
#     header `Authorization: Bearer ${SUITE_RUNTIME_TOKEN}` (the placeholder,
#     resolved by Hermes at connect time; the token itself never enters
#     config.yaml).
#
# Idempotent: a second run with the same arguments changes no file.
# The last line printed on success is exactly `hermes-suite-channel: installed`.

set -euo pipefail

readonly PLUGIN_NAME="startup-suite-platform"
readonly MCP_NAME="startup-suite"
readonly DEFAULT_REPO="https://github.com/Startup-Suite/hermes-suite-channel.git"
readonly RESULT_LINE="hermes-suite-channel: installed"

log() { printf 'hermes-suite-channel: %s\n' "$*" >&2; }
die() {
  printf 'hermes-suite-channel: error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: install.sh --url wss://<host>/runtime/ws --runtime-id <id> [options]

  --url URL            Suite runtime socket URL (ws:// or wss://, path /runtime/ws)
  --runtime-id ID      the federated runtime id registered in Suite
  --hermes-home DIR    Hermes home (default: $HERMES_HOME, else ~/.hermes)
  --token-file PATH    read the runtime token from PATH (else stdin, else a prompt)
  --mcp-url URL        Suite MCP endpoint (default: derived from --url, .../mcp)
  --allow-all-users    write SUITE_ALLOW_ALL_USERS=true (Suite already limits
                       delivery to spaces the agent participates in)
  --allowed-users CSV  write SUITE_ALLOWED_USERS=CSV (Suite display names)
  --hermes BIN         the hermes executable (default: hermes on PATH)
  --repo URL           plugin git repository, used when not run from a checkout
  --ref SHA            commit to install (default: the checkout's HEAD, or the
                       default branch of --repo)
  --copy-fallback      if `hermes plugins install` refuses the source, copy the
                       committed tree into $HERMES_HOME/plugins/ instead (this
                       skips Hermes's install-time scan and provenance record)
  --plugin-only        install/refresh the plugin files only; leave .env and the
                       MCP entry alone (used by update.sh)

The token is never accepted on the command line; --token is refused.
EOF
}

# ---------------------------------------------------------------- arguments

SUITE_URL_ARG=""
RUNTIME_ID_ARG=""
HERMES_HOME_ARG=""
TOKEN_FILE=""
MCP_URL_ARG=""
ALLOW_ALL=""
ALLOWED_USERS=""
HERMES_BIN="${HERMES_BIN:-hermes}"
REPO_URL="$DEFAULT_REPO"
REF_ARG=""
COPY_FALLBACK=0
PLUGIN_ONLY=0

refuse_token_arg() {
  die "--token is refused: a token on the command line is visible to every local user through ps and /proc/<pid>/cmdline. Pipe it on stdin (printf '%s' \"\$TOKEN\" | install.sh ...) or pass --token-file PATH (a file only you can read)."
}

need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --token | --token=*) refuse_token_arg ;;
      --url) need_value "$@"; SUITE_URL_ARG="$2"; shift 2 ;;
      --url=*) SUITE_URL_ARG="${1#*=}"; shift ;;
      --runtime-id) need_value "$@"; RUNTIME_ID_ARG="$2"; shift 2 ;;
      --runtime-id=*) RUNTIME_ID_ARG="${1#*=}"; shift ;;
      --hermes-home) need_value "$@"; HERMES_HOME_ARG="$2"; shift 2 ;;
      --hermes-home=*) HERMES_HOME_ARG="${1#*=}"; shift ;;
      --token-file) need_value "$@"; TOKEN_FILE="$2"; shift 2 ;;
      --token-file=*) TOKEN_FILE="${1#*=}"; shift ;;
      --mcp-url) need_value "$@"; MCP_URL_ARG="$2"; shift 2 ;;
      --mcp-url=*) MCP_URL_ARG="${1#*=}"; shift ;;
      --allow-all-users) ALLOW_ALL="true"; shift ;;
      --allowed-users) need_value "$@"; ALLOWED_USERS="$2"; shift 2 ;;
      --allowed-users=*) ALLOWED_USERS="${1#*=}"; shift ;;
      --hermes) need_value "$@"; HERMES_BIN="$2"; shift 2 ;;
      --hermes=*) HERMES_BIN="${1#*=}"; shift ;;
      --repo) need_value "$@"; REPO_URL="$2"; shift 2 ;;
      --repo=*) REPO_URL="${1#*=}"; shift ;;
      --ref) need_value "$@"; REF_ARG="$2"; shift 2 ;;
      --ref=*) REF_ARG="${1#*=}"; shift ;;
      --copy-fallback) COPY_FALLBACK=1; shift ;;
      --plugin-only) PLUGIN_ONLY=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage; die "unknown argument: $1" ;;
    esac
  done
  [ "$PLUGIN_ONLY" = 0 ] || return 0
  [ -n "$SUITE_URL_ARG" ] || { usage; die "--url is required"; }
  [ -n "$RUNTIME_ID_ARG" ] || { usage; die "--runtime-id is required"; }
}

# ------------------------------------------------------------ value checks

# Hermes writes a .env value unquoted unless it contains whitespace, '#' or a
# quote (hermes_cli/config.py _quote_env_value). Refusing those characters
# keeps every line this script writes byte-identical to what Hermes's own
# save_env_value would write, and keeps the line one line.
check_env_value() {
  local name="$1" value="$2"
  [ -n "$value" ] || die "$name is empty"
  local bad='[[:space:]#"'"'"'\\]'
  if [[ "$value" =~ $bad ]]; then
    die "$name contains whitespace, '#', a quote or a backslash; refusing to write it"
  fi
}

derive_mcp_url() {
  local url="$1" scheme rest host
  case "$url" in
    wss://*) scheme="https"; rest="${url#wss://}" ;;
    ws://*) scheme="http"; rest="${url#ws://}" ;;
    *) die "--url must start with ws:// or wss:// (got a different scheme)" ;;
  esac
  host="${rest%%/*}"
  [ -n "$host" ] || die "--url has no host"
  printf '%s://%s/mcp' "$scheme" "$host"
}

# ------------------------------------------------------------------- token

script_is_a_file() {
  # Under `curl | bash` (and `bash -s`, `bash < file`) BASH_SOURCE[0] is empty.
  [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]
}

# Sets the global TOKEN. Uses only bash builtins (read, printf), so the value
# is never on any process's argv.
TOKEN=""
read_token() {
  if [ -n "$TOKEN_FILE" ]; then
    [ -r "$TOKEN_FILE" ] || die "--token-file $TOKEN_FILE is not readable"
    IFS= read -r TOKEN <"$TOKEN_FILE" || true
  elif script_is_a_file && [ ! -t 0 ]; then
    IFS= read -r TOKEN || true
  elif { : </dev/tty; } 2>/dev/null; then
    printf 'Suite runtime token (input hidden): ' >/dev/tty
    IFS= read -rs TOKEN </dev/tty || true
    printf '\n' >/dev/tty
  else
    die "no token: pass --token-file PATH, or pipe the token on stdin to a downloaded install.sh"
  fi
  TOKEN="${TOKEN%$'\r'}"
  [ -n "$TOKEN" ] || die "the runtime token is empty"
  local bad='[^A-Za-z0-9._~+/=-]'
  if [[ "$TOKEN" =~ $bad ]]; then
    die "the runtime token contains characters outside [A-Za-z0-9._~+/=-]; refusing to write it"
  fi
}

# --------------------------------------------------------------- .env store

ENV_FILE=""
ENV_CHANGED=0

# Set KEY=VALUE in $ENV_FILE the way Hermes's save_env_value does: replace the
# first line that defines KEY (plain, `export KEY=`, or `KEY = v`), else
# append. Other lines are copied byte for byte. Bash builtins only.
env_set() {
  local key="$1" value="$2" line out="" found=0 changed=0
  local re="^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*="
  if [ -f "$ENV_FILE" ]; then
    # `|| [ -n "$line" ]` keeps a last line that has no newline; it gets one
    # only if this call rewrites the file (Hermes's writer does the same).
    while IFS= read -r line || [ -n "$line" ]; do
      if [ "$found" = 0 ] && [[ "$line" =~ $re ]]; then
        found=1
        [ "$line" = "${key}=${value}" ] || changed=1
        out+="${key}=${value}"$'\n'
      elif [[ "$line" =~ $re ]]; then
        changed=1 # a duplicate definition: dropped, as Hermes's writer drops it
      else
        out+="$line"$'\n'
      fi
    done <"$ENV_FILE"
  fi
  if [ "$found" = 0 ]; then
    out+="${key}=${value}"$'\n'
    changed=1
  fi
  [ "$changed" = 0 ] || write_env "$out"
}

write_env() {
  local content="$1" tmp
  tmp="$(umask 077 && mktemp "${ENV_FILE%/*}/.env_hsc.XXXXXX")"
  printf '%s' "$content" >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  ENV_CHANGED=1
}

# ------------------------------------------------------------------ hermes

run_hermes() {
  # stdin is /dev/null for every Hermes child: none of them may read (or be
  # handed) the token, and a non-interactive Hermes never blocks on a prompt.
  "$HERMES_BIN" "$@" </dev/null
}

plugin_dir() { printf '%s/plugins/%s' "$HERMES_HOME" "$PLUGIN_NAME"; }

stamped_build() {
  local f
  f="$(plugin_dir)/hermes_suite_channel/_build.py"
  [ -f "$f" ] || return 0
  sed -n 's/^BUILD = "\(.*\)"$/\1/p' "$f"
}

stamp_build() {
  local sha="$1" f tmp
  f="$(plugin_dir)/hermes_suite_channel/_build.py"
  [ -f "$f" ] || die "installed plugin has no hermes_suite_channel/_build.py"
  tmp="$(mktemp "$f.XXXXXX")"
  sed "s/^BUILD = \".*\"\$/BUILD = \"$sha\"/" "$f" >"$tmp"
  grep -q "^BUILD = \"$sha\"\$" "$tmp" || { rm -f "$tmp"; die "could not stamp _build.py"; }
  mv -f "$tmp" "$f"
}

plugin_enabled() {
  # Hermes prints plugins.enabled as a JSON list with --json.
  run_hermes config get plugins.enabled --json 2>/dev/null | grep -q "\"$PLUGIN_NAME\""
}

SRC_DIR=""
SRC_SHA=""
SRC_URL=""
CLEANUP_DIR=""
cleanup() { [ -z "$CLEANUP_DIR" ] || rm -rf "$CLEANUP_DIR"; }
trap cleanup EXIT

resolve_source() {
  local here=""
  if script_is_a_file; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$here" ] && [ -f "$here/plugin.yaml" ] && [ -d "$here/hermes_suite_channel" ] &&
    git -C "$here" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    SRC_DIR="$here"
    SRC_URL="file://$here"
    if [ -n "$REF_ARG" ]; then
      SRC_SHA="$(git -C "$here" rev-parse --verify -q "$REF_ARG^{commit}")" || die "no such commit: $REF_ARG"
    else
      SRC_SHA="$(git -C "$here" rev-parse HEAD)"
      if [ -n "$(git -C "$here" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        log "warning: $here has uncommitted changes; installing the committed HEAD $SRC_SHA"
      fi
    fi
    return
  fi
  # Remote install: clone with the caller's own git credentials (ssh agent,
  # credential helper, or gh auth setup-git). The repository is private.
  command -v git >/dev/null || die "git is required"
  CLEANUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-suite-channel.XXXXXX")"
  SRC_DIR="$CLEANUP_DIR/src"
  log "cloning $REPO_URL"
  git clone --quiet "$REPO_URL" "$SRC_DIR" ||
    die "could not clone $REPO_URL (private repository: authenticate git, e.g. gh auth setup-git, or pass --repo git@github.com:Startup-Suite/hermes-suite-channel.git)"
  if [ -n "$REF_ARG" ]; then
    git -C "$SRC_DIR" checkout --quiet --detach "$REF_ARG" || die "no such commit: $REF_ARG"
  fi
  SRC_SHA="$(git -C "$SRC_DIR" rev-parse HEAD)"
  SRC_URL="$REPO_URL"
}

# Copy the committed tree at $SRC_SHA into the plugins directory. Only with
# --copy-fallback, and only after `hermes plugins install` refused both the
# remote and the local clone. It is opt-in because Hermes can refuse for a
# reason that must not be bypassed (its install-time security scan), and the
# exit code does not say which. A copied plugin has no Hermes install
# metadata, so `hermes plugins update` does not manage it; update.sh does.
copy_fallback() {
  local target tmp
  target="$(plugin_dir)"
  mkdir -p "$HERMES_HOME/plugins"
  tmp="$(mktemp -d "$HERMES_HOME/plugins/.hsc-copy.XXXXXX")"
  git -C "$SRC_DIR" archive "$SRC_SHA" | tar -x -C "$tmp"
  rm -rf "$target"
  mv "$tmp" "$target"
}

install_plugin() {
  local current
  current="$(stamped_build)"
  if [ "$current" = "$SRC_SHA" ]; then
    log "plugin $PLUGIN_NAME already installed at $SRC_SHA"
  else
    local force=()
    [ ! -e "$(plugin_dir)" ] || force=(--force)
    log "installing plugin $PLUGIN_NAME at $SRC_SHA"
    if ! run_hermes plugins install "$SRC_URL" --ref "$SRC_SHA" --no-enable ${force[@]+"${force[@]}"} >&2; then
      if [ "$SRC_URL" != "file://$SRC_DIR" ] &&
        run_hermes plugins install "file://$SRC_DIR" --ref "$SRC_SHA" --no-enable ${force[@]+"${force[@]}"} >&2; then
        log "installed from the local clone (Hermes could not fetch $SRC_URL itself)"
      elif [ "$COPY_FALLBACK" = 1 ]; then
        log "hermes plugins install refused the source; copying into $HERMES_HOME/plugins/ (--copy-fallback)"
        copy_fallback
      else
        die "hermes plugins install refused the plugin (output above). If Hermes could not fetch a private repository, re-run with --copy-fallback to copy the committed tree into $HERMES_HOME/plugins/ instead."
      fi
    fi
    [ -f "$(plugin_dir)/plugin.yaml" ] || die "plugin did not land in $(plugin_dir)"
    stamp_build "$SRC_SHA"
  fi
  if plugin_enabled; then
    log "plugin $PLUGIN_NAME already enabled"
  else
    run_hermes plugins enable "$PLUGIN_NAME" >&2 || die "hermes plugins enable $PLUGIN_NAME failed"
    plugin_enabled || die "plugin $PLUGIN_NAME is still not enabled after hermes plugins enable"
  fi
}

config_get() { run_hermes config get "$1" --raw 2>/dev/null || true; }

config_set_if_changed() {
  local key="$1" value="$2"
  if [ "$(config_get "$key")" = "$value" ]; then
    return
  fi
  run_hermes config set "$key" "$value" >&2 || die "hermes config set $key failed"
}

register_mcp() {
  # The header value is the literal text `Bearer ${SUITE_RUNTIME_TOKEN}`:
  # Hermes resolves ${VAR} from the environment (.env included) at connect
  # time, so config.yaml never holds the token.
  local header='Bearer ${SUITE_RUNTIME_TOKEN}'
  config_set_if_changed "mcp_servers.$MCP_NAME.url" "$MCP_URL"
  config_set_if_changed "mcp_servers.$MCP_NAME.headers.Authorization" "$header"
}

# -------------------------------------------------------------------- main

main() {
  parse_args "$@"
  HERMES_HOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
  export HERMES_HOME
  command -v "$HERMES_BIN" >/dev/null 2>&1 ||
    die "hermes not found (looked for '$HERMES_BIN'); install Hermes Agent first or pass --hermes BIN"

  if [ "$PLUGIN_ONLY" = 1 ]; then
    resolve_source
    install_plugin
    return 0
  fi

  check_env_value SUITE_URL "$SUITE_URL_ARG"
  check_env_value SUITE_RUNTIME_ID "$RUNTIME_ID_ARG"
  [ -z "$ALLOWED_USERS" ] || check_env_value SUITE_ALLOWED_USERS "$ALLOWED_USERS"
  MCP_URL="${MCP_URL_ARG:-$(derive_mcp_url "$SUITE_URL_ARG")}"
  check_env_value "--mcp-url" "$MCP_URL"

  read_token

  mkdir -p "$HERMES_HOME"
  ENV_FILE="$HERMES_HOME/.env"

  resolve_source

  # The env keys go in first: `hermes plugins install` prompts for any
  # requires_env variable that is still unset.
  env_set SUITE_URL "$SUITE_URL_ARG"
  env_set SUITE_RUNTIME_ID "$RUNTIME_ID_ARG"
  env_set SUITE_RUNTIME_TOKEN "$TOKEN"
  TOKEN=""
  [ -z "$ALLOW_ALL" ] || env_set SUITE_ALLOW_ALL_USERS "$ALLOW_ALL"
  [ -z "$ALLOWED_USERS" ] || env_set SUITE_ALLOWED_USERS "$ALLOWED_USERS"
  [ "$ENV_CHANGED" = 0 ] || log "wrote Suite settings to $ENV_FILE (mode 0600)"
  chmod 600 "$ENV_FILE"

  install_plugin
  register_mcp

  log "restart the gateway to load it: hermes gateway restart"
  printf '%s\n' "$RESULT_LINE"
}

main "$@"
