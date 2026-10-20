"""Load this repository as a Hermes directory plugin with Hermes's OWN
manifest parser, loader and PluginContext (hermes_cli/plugins_manifest.py
``parse_manifest_file``, hermes_cli/plugins_loader.py
``_load_directory_module``), then register through the real
``PluginContext.register_platform``. This is what keeps the ``__package__``
guard in the root ``__init__.py`` honest: an entry point broken under Hermes
fails here even though pytest never sees it."""

from pathlib import Path

import pytest
import yaml

try:
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import parse_manifest_file
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError("set HERMES_AGENT_SRC to a hermes-agent checkout") from exc

from hermes_suite_channel.config import ENV_FALLBACKS

ROOT = Path(__file__).resolve().parent.parent


def test_hermes_loads_and_registers_the_plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    assert (manifest.name, manifest.kind) == ("burrow-platform", "platform")
    # No requires_env: Hermes prompts for each unset one at install, and an
    # entry names ONE spelling (see plugin.yaml and test_install_prompt_*).
    assert manifest.requires_env == []
    raw = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    for name in ("BURROW_RUNTIME_TOKEN", "SUITE_RUNTIME_TOKEN"):
        legacy = next(e for e in raw["optional_env"] if e["name"] == name)
        assert legacy["password"] is True and legacy["secret"] is True
    optional = [e["name"] for e in raw["optional_env"]]
    for new, old in ENV_FALLBACKS:
        if new in (
            "BURROW_HEARTBEAT_MS",
            "BURROW_WATCHDOG_INTERVAL_MS",
            "BURROW_PUSH_TIMEOUT_MS",
            "BURROW_JOIN_TIMEOUT_MS",
            "BURROW_DEDUPE_WINDOW_MS",
        ):
            continue  # internal tunables, never listed (as in v0.2.0)
        assert new in optional and old in optional, new

    manager = PluginManager()
    module = manager._load_directory_module(manifest)
    assert module.__name__ == "hermes_plugins.burrow_platform"
    assert callable(module.register)
    module.register(PluginContext(manifest, manager))
    entry = platform_registry.get("startup_suite")
    assert entry is not None and entry.label == "Burrow"
    assert entry.adapter_factory.__module__.startswith("hermes_plugins.burrow_platform.")


V020_ENV = (
    "SUITE_URL=wss://suite.example.test/runtime/ws\n"
    "SUITE_RUNTIME_ID=rt-01a0d6c5\n"
    "SUITE_RUNTIME_TOKEN_FILE=/h/mcp-tokens/burrow-platform.runtime-token\n"
)
BURROW_ONLY_ENV = V020_ENV.replace("SUITE_", "BURROW_")


@pytest.mark.parametrize(
    "dotenv",
    [V020_ENV, BURROW_ONLY_ENV, V020_ENV + BURROW_ONLY_ENV, ""],
    ids=["v0.2.0-suite-only", "burrow-only", "both", "empty"],
)
def test_install_prompt_asks_for_nothing(monkeypatch, tmp_path, dotenv):
    """`hermes plugins install` (update.sh runs it) prompts for every
    requires_env entry unset in $HERMES_HOME/.env, one spelling per entry
    (plugins_cmd.py _missing_env_specs, plugins_cmd_install.py
    _prompt_plugin_env_vars). Measured at fdec926e: v0.2.0's manifest asks
    nothing on a SUITE_-only .env, and the same manifest with BURROW_ names
    asks for BURROW_URL, BURROW_RUNTIME_ID and BURROW_RUNTIME_TOKEN_FILE. So
    this manifest declares none, and no host is prompted for either spelling.
    Drives Hermes's own prompt function and records every prompt it shows."""
    from hermes_cli import config as hermes_config
    from hermes_cli import plugins_cmd, plugins_cmd_install

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(dotenv)
    for new, old in ENV_FALLBACKS:
        monkeypatch.delenv(new, raising=False)
        monkeypatch.delenv(old, raising=False)
    prompts: list[str] = []

    def reader(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(plugins_cmd, "masked_secret_prompt", reader)
    monkeypatch.setattr(plugins_cmd_install, "line_input", reader)
    monkeypatch.setattr(hermes_config, "save_env_value", lambda *a: None)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())

    class Console:
        def print(self, *a, **k):
            pass

    assert plugins_cmd._missing_env_specs(manifest) == []
    plugins_cmd_install._prompt_plugin_env_vars(manifest, Console())
    assert prompts == []


def test_prompt_check_fires_on_a_manifest_that_requires_burrow_names(monkeypatch, tmp_path):
    """Positive control for the test above: the same Hermes function DOES
    report BURROW_ names missing on a v0.2.0 .env when a manifest requires
    them, so an empty result above is a measurement, not a dead probe."""
    from hermes_cli import plugins_cmd

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(V020_ENV)
    for new, old in ENV_FALLBACKS:
        monkeypatch.delenv(new, raising=False)
        monkeypatch.delenv(old, raising=False)
    naive = {"requires_env": ["BURROW_URL", "BURROW_RUNTIME_ID", "BURROW_RUNTIME_TOKEN_FILE"]}
    missing = [s["name"] for s in plugins_cmd._missing_env_specs(naive)]
    assert missing == ["BURROW_URL", "BURROW_RUNTIME_ID", "BURROW_RUNTIME_TOKEN_FILE"]
