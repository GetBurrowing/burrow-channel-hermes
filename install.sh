#!/usr/bin/env bash
# Install the Burrow channel plugin into a Hermes Agent home.
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
#   - $HERMES_HOME/mcp-tokens/burrow-platform.runtime-token: the token,
#     mode 0600. NOT .env: Hermes copies .env into the gateway's os.environ on
#     every turn, and children it spawns without a scrub inherit that (README,
#     "Security notes"). mcp-tokens/ is the directory Hermes's own file tools
#     and media delivery refuse to read.
#   - $HERMES_HOME/.env: BURROW_URL, BURROW_RUNTIME_ID, BURROW_RUNTIME_TOKEN_FILE
#     (the path above; plus BURROW_ALLOW_ALL_USERS / BURROW_ALLOWED_USERS when
#     asked), each ALSO written under its deprecated SUITE_ name so a
#     downgrade to v0.2.0 still connects; mode 0600. A BURROW_RUNTIME_TOKEN or
#     SUITE_RUNTIME_TOKEN line from an older install is removed.
#   - the plugin `burrow-platform`, installed and enabled through
#     `hermes plugins install` + `hermes plugins enable`;
#   - config.yaml `mcp_servers.burrow`: a stdio server running the
#     plugin's hermes_suite_channel/mcp_bridge.py, which relays to
#     https://<host>/mcp and reads the token from the file itself, so neither
#     config.yaml nor any environment holds it;
#   - $HERMES_HOME/.suite-channel/: the install record uninstall.sh uses to
#     give config.yaml back byte for byte (a 0600 copy of config.yaml as it
#     was before the first install, and which directories this created).
#
# Idempotent: a second run with the same arguments changes no file.
# The last line printed on success is exactly `hermes-suite-channel: installed`.

set -euo pipefail

readonly PLUGIN_NAME="burrow-platform"
readonly MCP_NAME="burrow"
readonly TOKEN_RELPATH="mcp-tokens/burrow-platform.runtime-token"
readonly RECORD_RELDIR=".suite-channel"
# Clone source when not run from a checkout. Precedence: --repo, then the
# HERMES_BURROW_CHANNEL_REPO environment variable, then the deprecated
# HERMES_SUITE_CHANNEL_REPO, then this default.
readonly DEFAULT_REPO="${HERMES_BURROW_CHANNEL_REPO:-${HERMES_SUITE_CHANNEL_REPO:-https://github.com/Startup-Suite/burrow-channel-hermes.git}}"
readonly RESULT_LINE="hermes-suite-channel: installed"

log() { printf 'hermes-suite-channel: %s\n' "$*" >&2; }
die() {
  printf 'hermes-suite-channel: error: %s\n' "$*" >&2
  exit 1
}

# One stderr line when only the deprecated repo override is set.
warn_deprecated_repo_env() {
  if [ -z "${HERMES_BURROW_CHANNEL_REPO:-}" ] && [ -n "${HERMES_SUITE_CHANNEL_REPO:-}" ]; then
    log "HERMES_SUITE_CHANNEL_REPO is deprecated; set HERMES_BURROW_CHANNEL_REPO (the old name still works)"
  fi
}

usage() {
  cat >&2 <<'EOF'
usage: install.sh --url wss://<host>/runtime/ws --runtime-id <id> [options]

  --url URL            Burrow runtime socket URL (ws:// or wss://, path /runtime/ws)
  --runtime-id ID      the federated runtime id registered in Burrow
  --hermes-home DIR    Hermes home (default: $HERMES_HOME, else ~/.hermes)
  --token-file PATH    read the runtime token from PATH (else stdin, else a prompt)
  --mcp-url URL        Burrow MCP endpoint (default: derived from --url, .../mcp)
  --allow-all-users    write BURROW_ALLOW_ALL_USERS=true (Burrow already limits
                       delivery to spaces the agent participates in)
  --allowed-users CSV  write BURROW_ALLOWED_USERS=CSV (Burrow display names)
  --hermes BIN         the hermes executable (default: hermes on PATH)
  --python PATH        the interpreter that reads config.yaml and runs the MCP
                       bridge (default: the launcher's python shebang, else
                       the environment Hermes selects, else python3 on PATH)
  --repo URL           plugin git repository, used when not run from a checkout
  --ref SHA            commit to install (default: the checkout's HEAD, or the
                       default branch of --repo)
  --copy-fallback      if `hermes plugins install` refuses the source, copy the
                       committed tree into $HERMES_HOME/plugins/ instead (this
                       skips Hermes's install-time scan and provenance record)
  --refuse-scan-caution  fail when Hermes's install-time scan returns CAUTION
                       (default: accept CAUTION with --force; DANGEROUS is
                       always refused, by Hermes itself)
  --plugin-only        install/refresh the plugin files only; leave .env and the
                       MCP entry alone (used by update.sh)

The token is never accepted on the command line; --token is refused.
EOF
}

# ---------------------------------------------------------------- arguments

URL_ARG=""
RUNTIME_ID_ARG=""
HERMES_HOME_ARG=""
TOKEN_FILE=""
MCP_URL_ARG=""
ALLOW_ALL=""
ALLOWED_USERS=""
HERMES_BIN="${HERMES_BIN:-hermes}"
PYTHON_ARG=""
REPO_URL="$DEFAULT_REPO"
REF_ARG=""
COPY_FALLBACK=0
PLUGIN_ONLY=0
REFUSE_CAUTION=0

refuse_token_arg() {
  die "--token is refused: a token on the command line is visible to every local user through ps and /proc/<pid>/cmdline. Pipe it on stdin (printf '%s' \"\$TOKEN\" | install.sh ...) or pass --token-file PATH (a file only you can read)."
}

need_value() { if [ "$#" -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value"; fi; }

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --token | --token=*) refuse_token_arg ;;
      --url) need_value "$@"; URL_ARG="$2"; shift 2 ;;
      --url=*) URL_ARG="${1#*=}"; shift ;;
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
      --python) need_value "$@"; PYTHON_ARG="$2"; shift 2 ;;
      --python=*) PYTHON_ARG="${1#*=}"; shift ;;
      --repo) need_value "$@"; REPO_URL="$2"; shift 2 ;;
      --repo=*) REPO_URL="${1#*=}"; shift ;;
      --ref) need_value "$@"; REF_ARG="$2"; shift 2 ;;
      --ref=*) REF_ARG="${1#*=}"; shift ;;
      --copy-fallback) COPY_FALLBACK=1; shift ;;
      --plugin-only) PLUGIN_ONLY=1; shift ;;
      --refuse-scan-caution) REFUSE_CAUTION=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage; die "unknown argument: $1" ;;
    esac
  done
  [ "$PLUGIN_ONLY" = 0 ] || return 0
  [ -n "$URL_ARG" ] || { usage; die "--url is required"; }
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
    printf 'Burrow runtime token (input hidden): ' >/dev/tty
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

# BURROW_<SUFFIX> and SUITE_<SUFFIX> to the same value.
env_set_both() {
  env_set "BURROW_$1" "$2"
  env_set "SUITE_$1" "$2"
}

# Remove every line defining KEY. Bash builtins only.
env_unset() {
  local key="$1" line out="" removed=0
  local re="^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*="
  [ -f "$ENV_FILE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ $re ]]; then
      removed=1
    else
      out+="$line"$'\n'
    fi
  done <"$ENV_FILE"
  [ "$removed" = 0 ] || write_env "$out"
}

write_env() {
  local content="$1" tmp
  tmp="$(umask 077 && mktemp "${ENV_FILE%/*}/.env_hsc.XXXXXX")"
  printf '%s' "$content" >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  ENV_CHANGED=1
}

# ------------------------------------------------------------ token file

TOKEN_PATH=""

# Write $TOKEN to $TOKEN_PATH (0600) unless the file already holds exactly
# that. The comparison is `cmp` on two files, so the stored token is never
# read into this shell or handed to a child on argv or stdout.
store_token() {
  local dir tmp
  dir="${TOKEN_PATH%/*}"
  if [ ! -d "$dir" ]; then
    (umask 077 && mkdir -p "$dir")
  fi
  tmp="$(umask 077 && mktemp "$dir/.hsc-token.XXXXXX")"
  printf '%s\n' "$TOKEN" >"$tmp"
  chmod 600 "$tmp"
  if [ -f "$TOKEN_PATH" ] && cmp -s "$tmp" "$TOKEN_PATH"; then
    rm -f "$tmp"
    return
  fi
  mv -f "$tmp" "$TOKEN_PATH"
  log "wrote the runtime token to $TOKEN_PATH (mode 0600)"
}

# ------------------------------------------------------------------ python

HPY=""

# The python of the dependency environment Hermes itself selects, asked of
# Hermes: `hermes --run-module pm.environments` prints Hermes's activation
# environment as JSON, whose PYTHONPATH is `<repo>:<venv>/lib/pythonX.Y/
# site-packages` (hermes-agent fdec926e). Prints nothing when this Hermes
# does not answer that way. The output is an environment: only PYTHONPATH is
# read from it, and none of it is printed.
hermes_venv_python() {
  local out pp entry parts=()
  out="$("$HERMES_BIN" --run-module pm.environments </dev/null 2>/dev/null)" || return 0
  [[ "$out" =~ \"PYTHONPATH\":\ \"([^\"]*)\" ]] || return 0
  pp="${BASH_REMATCH[1]}"
  IFS=: read -r -a parts <<<"$pp"
  for entry in ${parts[@]+"${parts[@]}"}; do
    if [[ "$entry" =~ ^(.*)/lib/python[0-9.]+/site-packages/?$ ]]; then
      [ ! -x "${BASH_REMATCH[1]}/bin/python" ] || printf '%s' "${BASH_REMATCH[1]}/bin/python"
      return 0
    fi
  done
}

# The interpreter that reads config.yaml (it needs Hermes's ruamel.yaml) and
# runs the MCP bridge (standard library only). In order:
#   1. --python PATH, when given (suite-cli passes the one it resolved);
#   2. the launcher's shebang, when that is a PYTHON. A shell shebang is a
#      wrapper, not an interpreter: every launcher hermes-agent fdec926e
#      installs is `#!/bin/sh`, and taking /bin/sh for python is the failure
#      this order exists to prevent;
#   3. the dependency environment Hermes selects (hermes_venv_python);
#   4. python3 on PATH.
find_hermes_python() {
  local bin line="" interp="" words=()
  if [ -n "$PYTHON_ARG" ]; then
    interp="$PYTHON_ARG"
    [ -x "$interp" ] || die "--python $interp is not an executable file"
  else
    bin="$(command -v "$HERMES_BIN")"
    if [ -f "$bin" ]; then
      IFS= read -r line <"$bin" || true
    fi
    if [[ "$line" == '#!'* ]]; then
      read -r -a words <<<"${line#??}"
      if [ "${#words[@]}" -gt 0 ]; then
        interp="${words[0]}"
        if [ "${interp##*/}" = env ] && [ "${#words[@]}" -gt 1 ]; then
          interp="$(command -v "${words[1]}" || true)"
        fi
      fi
    fi
    case "${interp##*/}" in
      python*) ;;
      *) interp="" ;;
    esac
    if [ -z "$interp" ] || [ ! -x "$interp" ]; then
      interp="$(hermes_venv_python)"
    fi
    if [ -z "$interp" ] || [ ! -x "$interp" ]; then
      interp="$(command -v python3 || true)"
    fi
  fi
  [ -n "$interp" ] || die "no python3 found (needed for the MCP bridge and to read config.yaml); pass --python PATH"
  "$interp" -c 'import sys; sys.exit(sys.version_info < (3, 8))' ||
    die "$interp is older than Python 3.8 (or is not a Python); pass --python PATH"
  HPY="$interp"
}

# Read-only questions about config.yaml, answered from the RAW YAML: never
# `hermes config get --raw`, which expands ${VAR} placeholders from the
# environment. Exit 0 = yes, 1 = no, 4 = no YAML library or unparseable.
#   is KEY VALUE   the dotted KEY holds VALUE (a string, or a JSON list)
#   has KEY        the dotted KEY is set
#   json ARG...    print ARG... as a JSON list (reads no config)
yaml_q() {
  "$HPY" - "$HERMES_HOME/config.yaml" "$@" <<'PYEOF'
import json, sys
path, op, args = sys.argv[1], sys.argv[2], sys.argv[3:]
if op == "json":
    print(json.dumps(args))
    sys.exit(0)
try:
    from ruamel.yaml import YAML

    def load(text):
        return YAML(typ="safe", pure=True).load(text)
except ImportError:
    try:
        import yaml

        load = yaml.safe_load
    except ImportError:
        sys.exit(4)
try:
    with open(path, encoding="utf-8") as f:
        data = load(f.read())
except FileNotFoundError:
    data = None
except Exception:
    sys.exit(4)
node = data
for part in args[0].split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit(1)
    node = node[part]
if op == "has":
    sys.exit(0)
want = args[1]
if isinstance(node, list):
    try:
        sys.exit(0 if node == json.loads(want) else 1)
    except ValueError:
        sys.exit(1)
sys.exit(0 if isinstance(node, str) and node == want else 1)
PYEOF
}

# ---------------------------------------------------------- install record

# Written once, before the first install touches anything, and kept until
# uninstall.sh: a 0600 copy of config.yaml as it was, and which directories
# did not exist yet. An install over an older one that left no record says
# so (pre_install_known=0), and uninstall then removes only its own keys.
write_install_record() {
  local dir="$HERMES_HOME/$RECORD_RELDIR" known=1 existed=0 tokdir=0 envfile=0
  [ ! -f "$dir/record" ] || return 0
  if [ -e "$(plugin_dir)" ] || yaml_q has "mcp_servers.$MCP_NAME"; then
    known=0
  fi
  (umask 077 && mkdir -p "$dir")
  if [ -f "$HERMES_HOME/config.yaml" ]; then
    existed=1
    (umask 077 && cp "$HERMES_HOME/config.yaml" "$dir/config.yaml.pre-install")
    chmod 600 "$dir/config.yaml.pre-install"
  fi
  [ ! -d "$HERMES_HOME/mcp-tokens" ] || tokdir=1
  [ ! -e "$HERMES_HOME/.env" ] || envfile=1
  (
    umask 077
    printf 'pre_install_known=%s\nconfig_existed=%s\nmcp_tokens_existed=%s\nenv_existed=%s\n' \
      "$known" "$existed" "$tokdir" "$envfile" >"$dir/record"
  )
}

# ------------------------------------------------------------------ hermes

run_hermes() {
  # stdin is /dev/null for every Hermes child: none of them may read (or be
  # handed) the token, and a non-interactive Hermes never blocks on a prompt.
  # COLUMNS keeps Hermes's rich output from wrapping the scan verdict line.
  COLUMNS=200 "$HERMES_BIN" "$@" </dev/null
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
# 1 between `hermes plugins disable` (for a reinstall over an enabled plugin)
# and the `hermes plugins enable` that follows it. Any exit in that window,
# a die() included, re-enables the plugin at whatever version is on disk:
# Hermes leaves the installed tree unchanged when it refuses an install, so
# that is the version that was running. An upgrade attempt must never leave a
# working host with its channel disabled.
REENABLE_ON_EXIT=0
reenable_after_failure() {
  log "re-enabling plugin $PLUGIN_NAME at the version still installed ($(stamped_build || true))"
  if run_hermes plugins enable "$PLUGIN_NAME" >&2 && plugin_enabled; then
    log "plugin $PLUGIN_NAME re-enabled; the host is as it was before this run"
  else
    log "error: could not re-enable plugin $PLUGIN_NAME; run: hermes plugins enable $PLUGIN_NAME"
  fi
}
cleanup() {
  if [ "$REENABLE_ON_EXIT" = 1 ]; then
    REENABLE_ON_EXIT=0
    reenable_after_failure || true
  fi
  [ -z "$CLEANUP_DIR" ] || rm -rf "$CLEANUP_DIR"
}
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
    die "could not clone $REPO_URL (private repository: authenticate git, e.g. gh auth setup-git, or pass the ssh form of $REPO_URL with --repo URL or HERMES_BURROW_CHANNEL_REPO=URL)"
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

LAST_INSTALL_OUT=""

# One `hermes plugins install`, its output passed through to stderr and kept
# for try_install to read.
hermes_install() {
  local src="$1" rc=0
  shift
  LAST_INSTALL_OUT="$(run_hermes plugins install "$src" --ref "$SRC_SHA" --no-enable "$@" 2>&1)" || rc=$?
  printf '%s\n' "$LAST_INSTALL_OUT" >&2
  return "$rc"
}

# Hermes scans every plugin tree at install time: CAUTION asks for
# confirmation (a non-interactive install is refused), DANGEROUS is refused
# even with --force (tools/plugin_guard.py). This repository's tree scans
# CAUTION because its README documents the curl|bash install of this very
# plugin, so a CAUTION verdict is accepted with --force, loudly, unless
# --refuse-scan-caution. A DANGEROUS verdict is never retried.
try_install() {
  local src="$1"
  shift
  hermes_install "$src" "$@" && return 0
  case "$LAST_INSTALL_OUT" in
    *"Verdict: DANGEROUS"* | *"Verdict: dangerous"*) return 1 ;;
    *"Verdict: CAUTION"* | *"Verdict: caution"*) ;;
    *) return 1 ;;
  esac
  if [ "$REFUSE_CAUTION" = 1 ]; then
    log "Hermes's install scan returned CAUTION and --refuse-scan-caution is set"
    return 1
  fi
  log "Hermes's install scan returned CAUTION for $SRC_SHA (findings above). Accepting it with --force; pass --refuse-scan-caution to stop here instead."
  hermes_install "$src" --force
}

install_plugin() {
  local current
  current="$(stamped_build)"
  if [ "$current" = "$SRC_SHA" ]; then
    log "plugin $PLUGIN_NAME already installed at $SRC_SHA"
  else
    local force=()
    [ ! -e "$(plugin_dir)" ] || force=(--force)
    # Hermes (hermes-agent fdec926e, plugins_transaction.publish_plugin)
    # refuses to replace an ENABLED plugin that declares Python dependencies
    # unless a terminal user consents, and every Hermes child here runs with
    # stdin on /dev/null: "Reinstall declined: dependency install skipped
    # (non-interactive)". `hermes plugins install` has no consent flag. A
    # disabled plugin's dependencies are admitted by `hermes plugins enable`
    # instead, which needs no terminal, so: disable, install, enable.
    if [ -e "$(plugin_dir)" ] && plugin_enabled; then
      log "disabling plugin $PLUGIN_NAME for the reinstall (Hermes refuses to replace an enabled plugin non-interactively); it is re-enabled below, or on any failure"
      run_hermes plugins disable "$PLUGIN_NAME" >&2 || die "hermes plugins disable $PLUGIN_NAME failed"
      REENABLE_ON_EXIT=1
    fi
    log "installing plugin $PLUGIN_NAME at $SRC_SHA"
    if ! try_install "$SRC_URL" ${force[@]+"${force[@]}"}; then
      if [ "$SRC_URL" != "file://$SRC_DIR" ] &&
        try_install "file://$SRC_DIR" ${force[@]+"${force[@]}"}; then
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
  REENABLE_ON_EXIT=0
}

# Set KEY to VALUE unless the raw YAML already holds it. VALUE is a string or
# a JSON list (the form `hermes config set` coerces to a list).
YAML_UNKNOWN_SAID=0
config_set_if_changed() {
  local key="$1" value="$2" rc=0
  yaml_q is "$key" "$value" || rc=$?
  [ "$rc" != 0 ] || return 0
  if [ "$rc" = 4 ] && [ "$YAML_UNKNOWN_SAID" = 0 ]; then
    YAML_UNKNOWN_SAID=1
    log "warning: $HPY cannot parse config.yaml (no YAML library?); setting every MCP key"
  fi
  run_hermes config set "$key" "$value" >&2 || die "hermes config set $key failed"
}

config_unset_if_set() {
  yaml_q has "$1" || return 0
  run_hermes config unset "$1" >&2 || die "hermes config unset $1 failed"
}

register_mcp() {
  # A stdio server: Hermes spawns the bridge with a filtered environment and
  # the bridge reads the token from the file, so no environment and no
  # config.yaml value holds it. The http form an older install wrote (url
  # plus an Authorization header interpolating ${SUITE_RUNTIME_TOKEN}) is
  # removed: it works only with the token in os.environ.
  local args
  config_unset_if_set "mcp_servers.$MCP_NAME.url"
  config_unset_if_set "mcp_servers.$MCP_NAME.headers"
  args="$(yaml_q json "$(plugin_dir)/hermes_suite_channel/mcp_bridge.py" \
    --url "$MCP_URL" --token-file "$TOKEN_PATH")"
  config_set_if_changed "mcp_servers.$MCP_NAME.command" "$HPY"
  config_set_if_changed "mcp_servers.$MCP_NAME.args" "$args"
}

# -------------------------------------------------------------------- main

main() {
  parse_args "$@"
  warn_deprecated_repo_env
  HERMES_HOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
  export HERMES_HOME
  command -v "$HERMES_BIN" >/dev/null 2>&1 ||
    die "hermes not found (looked for '$HERMES_BIN'); install Hermes Agent first or pass --hermes BIN"

  if [ "$PLUGIN_ONLY" = 1 ]; then
    resolve_source
    install_plugin
    return 0
  fi

  check_env_value BURROW_URL "$URL_ARG"
  check_env_value BURROW_RUNTIME_ID "$RUNTIME_ID_ARG"
  [ -z "$ALLOWED_USERS" ] || check_env_value BURROW_ALLOWED_USERS "$ALLOWED_USERS"
  MCP_URL="${MCP_URL_ARG:-$(derive_mcp_url "$URL_ARG")}"
  check_env_value "--mcp-url" "$MCP_URL"

  read_token

  mkdir -p "$HERMES_HOME"
  ENV_FILE="$HERMES_HOME/.env"
  TOKEN_PATH="$HERMES_HOME/$TOKEN_RELPATH"
  check_env_value BURROW_RUNTIME_TOKEN_FILE "$TOKEN_PATH"

  find_hermes_python
  resolve_source
  write_install_record

  store_token
  TOKEN=""

  # The env keys go in first: `hermes plugins install` prompts for any
  # requires_env variable that is still unset (v0.2.0's plugin.yaml lists
  # the SUITE_ names). Every key is written under BOTH names: the plugin reads
  # BURROW_ first, and v0.2.0 reads only SUITE_, so a downgrade still works.
  env_set_both URL "$URL_ARG"
  env_set_both RUNTIME_ID "$RUNTIME_ID_ARG"
  env_set_both RUNTIME_TOKEN_FILE "$TOKEN_PATH"
  env_unset BURROW_RUNTIME_TOKEN
  env_unset SUITE_RUNTIME_TOKEN
  [ -z "$ALLOW_ALL" ] || env_set_both ALLOW_ALL_USERS "$ALLOW_ALL"
  [ -z "$ALLOWED_USERS" ] || env_set_both ALLOWED_USERS "$ALLOWED_USERS"
  [ "$ENV_CHANGED" = 0 ] || log "wrote Burrow settings to $ENV_FILE (mode 0600)"
  chmod 600 "$ENV_FILE"

  install_plugin
  register_mcp

  log "restart the gateway to load it: hermes gateway restart"
  printf '%s\n' "$RESULT_LINE"
}

main "$@"
