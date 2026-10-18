#!/usr/bin/env bash
# Update an installed Burrow channel plugin to this checkout's HEAD
# (or, outside a checkout, to --ref / the default branch of --repo).
#
# Mirrors burrow-channel-openclaw's update.sh: it refreshes the plugin files
# only and preserves the configuration. .env (URL, runtime id, token) and the
# MCP entry are left untouched; the token is not needed and not read.
#
#   ./update.sh [--hermes-home DIR] [--hermes BIN]
#   curl -fsSL <raw url>/update.sh | bash -s -- [--ref SHA] [--repo URL]
#
# The last line printed on success is exactly `hermes-suite-channel: updated`.

set -euo pipefail

readonly PLUGIN_NAME="startup-suite-platform"
# Clone source when not run from a checkout. Precedence: --repo, then the
# HERMES_BURROW_CHANNEL_REPO environment variable, then the deprecated
# HERMES_SUITE_CHANNEL_REPO, then this default.
readonly DEFAULT_REPO="${HERMES_BURROW_CHANNEL_REPO:-${HERMES_SUITE_CHANNEL_REPO:-https://github.com/Startup-Suite/burrow-channel-hermes.git}}"
readonly RESULT_LINE="hermes-suite-channel: updated"

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
warn_deprecated_repo_env

HERMES_HOME_ARG=""
REPO_URL="$DEFAULT_REPO"
PASS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --token | --token=*) die "--token is refused, and update.sh does not need the token" ;;
    --hermes-home) [ -n "${2:-}" ] || die "--hermes-home needs a value"; HERMES_HOME_ARG="$2"; shift 2 ;;
    --hermes-home=*) HERMES_HOME_ARG="${1#*=}"; shift ;;
    --repo) [ -n "${2:-}" ] || die "--repo needs a value"; REPO_URL="$2"; shift 2 ;;
    --hermes | --ref) [ -n "${2:-}" ] || die "$1 needs a value"; PASS+=("$1" "$2"); shift 2 ;;
    --repo=*) REPO_URL="${1#*=}"; shift ;;
    --hermes=* | --ref=*) PASS+=("$1"); shift ;;
    --copy-fallback | --refuse-scan-caution) PASS+=("$1"); shift ;;
    -h | --help)
      echo "usage: update.sh [--hermes-home DIR] [--hermes BIN] [--ref SHA] [--repo URL] [--copy-fallback] [--refuse-scan-caution]" >&2
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Hand install.sh the resolved repository (flag, else env, else default) so
# both scripts clone the same source.
PASS+=(--repo "$REPO_URL")

HERMES_HOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
export HERMES_HOME
PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_NAME"
if [ ! -d "$PLUGIN_DIR" ]; then
  die "plugin not installed at $PLUGIN_DIR; run install.sh first"
fi

# install.sh --plugin-only does the work: same source resolution, same
# `hermes plugins install --force --ref <sha>`, same build stamp.
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
CLEANUP_DIR=""
trap '[ -z "$CLEANUP_DIR" ] || rm -rf "$CLEANUP_DIR"' EXIT
if [ -n "$HERE" ] && [ -f "$HERE/install.sh" ] && [ -f "$HERE/plugin.yaml" ]; then
  INSTALLER="$HERE/install.sh"
else
  CLEANUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-suite-channel.XXXXXX")"
  git clone --quiet "$REPO_URL" "$CLEANUP_DIR/src" ||
    die "could not clone $REPO_URL (private repository: authenticate git first)"
  INSTALLER="$CLEANUP_DIR/src/install.sh"
fi

BEFORE="$(sed -n 's/^BUILD = "\(.*\)"$/\1/p' "$PLUGIN_DIR/hermes_suite_channel/_build.py" 2>/dev/null || true)"
# The repository is already resolved into --repo, so the child does not need
# the old override, and without it does not print the deprecation line twice.
env -u HERMES_SUITE_CHANNEL_REPO \
  bash "$INSTALLER" --plugin-only --hermes-home "$HERMES_HOME" ${PASS[@]+"${PASS[@]}"} </dev/null
AFTER="$(sed -n 's/^BUILD = "\(.*\)"$/\1/p' "$PLUGIN_DIR/hermes_suite_channel/_build.py")"

if [ "$BEFORE" = "$AFTER" ]; then
  log "already at $AFTER"
else
  log "updated ${BEFORE:-unstamped} -> $AFTER; restart the gateway to load it: hermes gateway restart"
fi
printf '%s\n' "$RESULT_LINE"
