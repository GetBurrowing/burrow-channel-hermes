#!/usr/bin/env bash
# Remove what install.sh added to a Hermes Agent home, and nothing else:
#   - config.yaml `mcp_servers.startup-suite` (through `hermes config unset`);
#   - the plugin `startup-suite-platform` (through `hermes plugins remove`);
#   - the bookkeeping Hermes's own writer added to config.yaml on the way
#     (`plugins: {enabled: [], disabled: []}`, `_config_version`), when the
#     install record shows it was not there before: config.yaml goes back to
#     the saved pre-install bytes when every other key still matches, and
#     otherwise (the file was edited since) only those keys are removed;
#   - the token file $HERMES_HOME/mcp-tokens/startup-suite-platform.runtime-token
#     (and mcp-tokens/ itself, if the install created it and it is empty);
#   - the install record $HERMES_HOME/.suite-channel/;
#   - the .env lines defining SUITE_URL, SUITE_RUNTIME_ID, SUITE_RUNTIME_TOKEN_FILE,
#     SUITE_RUNTIME_TOKEN, SUITE_ALLOW_ALL_USERS and SUITE_ALLOWED_USERS.
#     Every other .env line is kept byte for byte; a .env the install created
#     is removed when nothing else is left in it.
#
# Hermes's own config snapshots ($HERMES_HOME/backups/config/config.yaml.good.*,
# written by `hermes config set`) are Hermes's files, not this plugin's, and
# are left alone.
#
#   ./uninstall.sh [--hermes-home DIR] [--hermes BIN]
#   curl -fsSL <raw url>/uninstall.sh | bash -s -- [--hermes-home DIR]
#
# Safe to run twice: the second run finds nothing and changes nothing.
# The last line printed on success is exactly `hermes-suite-channel: uninstalled`.

set -euo pipefail

readonly PLUGIN_NAME="startup-suite-platform"
readonly MCP_NAME="startup-suite"
readonly ENV_KEYS=(SUITE_URL SUITE_RUNTIME_ID SUITE_RUNTIME_TOKEN_FILE SUITE_RUNTIME_TOKEN SUITE_ALLOW_ALL_USERS SUITE_ALLOWED_USERS)
readonly TOKEN_RELPATH="mcp-tokens/startup-suite-platform.runtime-token"
readonly RECORD_RELDIR=".suite-channel"
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

# find_hermes_python and the yaml_q `has` question: the same code as in
# install.sh (tests/test_install_scripts.py checks the copies are identical;
# a `curl | bash` uninstall has no checkout to source a shared file from).
HPY=""
find_hermes_python() {
  local bin line="" interp="" words=()
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
  if [ -z "$interp" ] || [ ! -x "$interp" ]; then
    interp="$(command -v python3 || true)"
  fi
  [ -n "$interp" ] || die "no python3 found (needed for the MCP bridge and to read config.yaml)"
  "$interp" -c 'import sys; sys.exit(sys.version_info < (3, 8))' ||
    die "$interp is older than Python 3.8"
  HPY="$interp"
}

# Exit 0 when the dotted key is set in the RAW config.yaml, 1 when not, 4
# when it cannot be parsed (never `hermes config get --raw`, which expands
# ${VAR} placeholders from the environment).
yaml_has() {
  "$HPY" - "$HERMES_HOME/config.yaml" "$1" <<'PYEOF'
import sys
path, key = sys.argv[1], sys.argv[2]
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
        node = load(f.read())
except FileNotFoundError:
    sys.exit(1)
except Exception:
    sys.exit(4)
for part in key.split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit(1)
    node = node[part]
sys.exit(0)
PYEOF
}

find_hermes_python

# 1. The MCP entry. Removed first: it names the token file.
mcp_rc=0
yaml_has "mcp_servers.$MCP_NAME" || mcp_rc=$?
if [ "$mcp_rc" != 1 ]; then
  # 4 (unparseable) tries the unset too; Hermes says "not set" and that is fine.
  if run_hermes config unset "mcp_servers.$MCP_NAME" >&2; then
    log "removed MCP server $MCP_NAME"
  elif [ "$mcp_rc" = 0 ]; then
    die "hermes config unset mcp_servers.$MCP_NAME failed"
  fi
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

# 3. What Hermes's writer added to config.yaml while installing: `plugins:`
# with empty enabled/disabled lists and a `_config_version` stamp. Only keys
# the install record shows were ABSENT before are candidates, and only in
# their empty shape. If config.yaml minus those equals the saved pre-install
# file, the saved bytes are put back (comments and layout included);
# otherwise the file was edited since, and just those lines are removed.
RECORD_DIR="$HERMES_HOME/$RECORD_RELDIR"
if [ -f "$RECORD_DIR/record" ]; then
  "$HPY" - "$HERMES_HOME/config.yaml" "$RECORD_DIR" <<'PYEOF' >&2
import os, re, sys

cfg_path, record_dir = sys.argv[1], sys.argv[2]
record = {}
with open(os.path.join(record_dir, "record"), encoding="utf-8") as f:
    for line in f:
        key, _, value = line.strip().partition("=")
        record[key] = value
say = lambda msg: print(f"hermes-suite-channel: {msg}")
if record.get("pre_install_known") != "1":
    say("config.yaml: no pre-install record (installed over an older version); left as is")
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
        say("config.yaml: no YAML library, so its bookkeeping keys were left in place")
        sys.exit(0)

pre_copy = os.path.join(record_dir, "config.yaml.pre-install")
existed = record.get("config_existed") == "1"
pre_text = open(pre_copy, encoding="utf-8", newline="").read() if existed else ""
try:
    cur_text = open(cfg_path, encoding="utf-8", newline="").read()
except FileNotFoundError:
    cur_text = None
if cur_text is None:
    say("config.yaml is gone; nothing to restore")
    sys.exit(0)
pre = load(pre_text) or {}
cur = load(cur_text) or {}
if not isinstance(pre, dict) or not isinstance(cur, dict):
    say("config.yaml is not a mapping; left as is")
    sys.exit(0)


def empty_plugins(value):
    return isinstance(value, dict) and set(value) <= {"enabled", "disabled"} and all(
        v in ([], None) for v in value.values()
    )


def without_bookkeeping(data):
    data = dict(data)
    removable = []
    if "_config_version" not in pre and "_config_version" in data:
        del data["_config_version"]
        removable.append("_config_version")
    if "plugins" not in pre and "plugins" in data and empty_plugins(data["plugins"]):
        del data["plugins"]
        removable.append("plugins")
    elif isinstance(pre.get("plugins"), dict) and isinstance(data.get("plugins"), dict):
        plugins = dict(data["plugins"])
        for sub in ("enabled", "disabled"):
            if sub not in pre["plugins"] and plugins.get(sub) == []:
                del plugins[sub]
        data["plugins"] = plugins
    return data, removable


def write_atomic(text):
    mode = os.stat(cfg_path).st_mode & 0o7777
    tmp = f"{cfg_path}.hsc-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, cfg_path)


stripped, removable = without_bookkeeping(cur)
if stripped == pre:
    if cur_text == pre_text:
        sys.exit(0)
    if existed:
        with open(pre_copy, encoding="utf-8", newline="") as f:
            write_atomic(f.read())
        say("config.yaml restored byte for byte from the pre-install copy")
    else:
        os.unlink(cfg_path)
        say("config.yaml removed: it did not exist before the install and holds nothing else")
    sys.exit(0)

# Edited since the install: remove only the absent-before bookkeeping lines.
lines = cur_text.splitlines(keepends=True)
out, i, dropped = [], 0, []
top = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")
while i < len(lines):
    m = top.match(lines[i])
    if m and m.group(1) in removable:
        j = i + 1
        while j < len(lines) and (lines[j].strip() == "" or lines[j][:1] in (" ", "\t")):
            j += 1
        block = "".join(lines[i:j])
        value = (load(block) or {}).get(m.group(1))
        if m.group(1) == "_config_version" or empty_plugins(value):
            # keep the blank lines that separated the block from what follows
            tail = [ln for ln in lines[i + 1 : j] if ln.strip() == ""]
            out.extend(tail if j < len(lines) else [])
            dropped.append(m.group(1))
            i = j
            continue
    out.append(lines[i])
    i += 1
if dropped:
    write_atomic("".join(out))
say(
    "config.yaml was edited after the install, so it was not restored from the pre-install "
    "copy; removed only: " + (", ".join(dropped) or "nothing")
)
PYEOF
fi

# 4. The token file, then the record.
TOKEN_FILE_PATH="$HERMES_HOME/$TOKEN_RELPATH"
if [ -e "$TOKEN_FILE_PATH" ]; then
  rm -f "$TOKEN_FILE_PATH"
  log "removed the token file $TOKEN_FILE_PATH"
fi
ENV_CREATED=0
if [ -f "$RECORD_DIR/record" ]; then
  if grep -qx 'mcp_tokens_existed=0' "$RECORD_DIR/record"; then
    rmdir "$HERMES_HOME/mcp-tokens" 2>/dev/null || true
  fi
  if grep -qx 'env_existed=0' "$RECORD_DIR/record"; then
    ENV_CREATED=1
  fi
fi
if [ -d "$RECORD_DIR" ]; then
  rm -rf "$RECORD_DIR"
  log "removed the install record $RECORD_DIR"
fi

# 5. The .env keys. Bash builtins only; lines are compared by key, so no value
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
  # install.sh created .env and nothing else has written to it since.
  if [ "$ENV_CREATED" = 1 ] && [ ! -s "$ENV_FILE" ]; then
    rm -f "$ENV_FILE"
    log "removed $ENV_FILE (the install created it and it is empty now)"
  fi
fi

log "restart the gateway if it is running: hermes gateway restart"
printf '%s\n' "$RESULT_LINE"
