"""The plugin must not import any path Hermes lists as removed.

From 2026-09-14 Hermes's loader refuses a plugin whose source imports a name
from a pre-decomposition module path (hermes_cli/plugins_loader.py
``_load_plugin_scoped`` -> hermes_cli/plugin_compat.py ``disable_reason``).
The refusal is the whole plugin, not one call site: v0.5.0 imported
``gateway.status.is_gateway_running`` lazily, inside one migration helper, and
the gateway still came up with "No messaging platforms enabled".

tests/test_plugin_load.py did not catch that because it calls
``_load_directory_module`` directly, which skips the compat gate. These tests
run Hermes's OWN scanner and gate, from the checkout CI pins, over this
repository exactly as the loader does (tests/ excluded by the scanner itself),
with a positive control so an empty manifest or a scanner that no longer
matches cannot pass them silently.
"""

import datetime
from pathlib import Path

import pytest

try:
    from hermes_cli import plugin_compat
    from hermes_cli.plugins_manifest import parse_manifest_file
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError("set HERMES_AGENT_SRC to a hermes-agent checkout") from exc

ROOT = Path(__file__).resolve().parent.parent
# A day the removal is in effect, so disable_reason() answers as it does now.
AFTER_REMOVAL = plugin_compat.COMPAT_REMOVAL_DATE + datetime.timedelta(days=1)


@pytest.fixture
def manifest():
    entries = plugin_compat.load_manifest()
    # The checkout must still carry the compat manifest, or every scan below
    # is vacuously empty. When Hermes reverts the layer this fails on purpose:
    # pin a checkout that still has compat_manifest.json, or replace these
    # tests with a scan against the revert's own list.
    assert "is_gateway_running" in entries.get("gateway.status", {})
    return entries


def test_the_plugin_imports_no_path_hermes_removed(manifest):
    hits = plugin_compat.scan_plugin(ROOT, manifest)
    assert hits == [], "\n".join(f"{h.file}:{h.line} {h.old} -> {h.new}" for h in hits)


def test_hermes_would_load_the_plugin_after_the_removal_date(manifest, monkeypatch, tmp_path):
    # An empty HERMES_HOME: no config.yaml, so allow_deprecated_imports is off.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert plugin_compat.allow_deprecated_imports() is False
    plugin = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    assert plugin_compat.disable_reason(plugin, today=AFTER_REMOVAL) is None


def test_the_scan_fires_on_the_import_v050_shipped(manifest, monkeypatch, tmp_path):
    """Positive control: a copy of v0.5.0's lazy import, inside a function,
    in a subpackage, is flagged by the same scanner and disables the plugin
    through the same gate."""
    (tmp_path / "plugin.yaml").write_text((ROOT / "plugin.yaml").read_text())
    pkg = tmp_path / "hermes_burrow_channel"
    pkg.mkdir()
    (pkg / "platform_rename.py").write_text(
        "def gateway_running(home):\n"
        "    from gateway.status import is_gateway_running\n"
        "    return is_gateway_running(home / 'gateway.pid', cleanup_stale=False)\n"
    )
    hits = plugin_compat.scan_plugin(tmp_path, manifest)
    assert [(h.file, h.line, h.old) for h in hits] == [
        ("hermes_burrow_channel/platform_rename.py", 2, "gateway.status.is_gateway_running")
    ]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugin = parse_manifest_file(tmp_path / "plugin.yaml", tmp_path, "user", "")
    reason = plugin_compat.disable_reason(plugin, today=AFTER_REMOVAL)
    assert reason is not None and "1 import path(s) removed" in reason
