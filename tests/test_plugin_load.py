"""Load this repository as a Hermes directory plugin with Hermes's OWN
manifest parser, loader and PluginContext (hermes_cli/plugins_manifest.py
``parse_manifest_file``, hermes_cli/plugins_loader.py
``_load_directory_module``), then register through the real
``PluginContext.register_platform``. This is what keeps the ``__package__``
guard in the root ``__init__.py`` honest: an entry point broken under Hermes
fails here even though pytest never sees it."""

from pathlib import Path

try:
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import parse_manifest_file
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError("set HERMES_AGENT_SRC to a hermes-agent checkout") from exc

ROOT = Path(__file__).resolve().parent.parent


def test_hermes_loads_and_registers_the_plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    assert (manifest.name, manifest.kind) == ("startup-suite-platform", "platform")
    names = [e["name"] if isinstance(e, dict) else e for e in manifest.requires_env]
    assert names == ["SUITE_URL", "SUITE_RUNTIME_ID", "SUITE_RUNTIME_TOKEN"]
    token_entry = next(e for e in manifest.requires_env if e["name"] == "SUITE_RUNTIME_TOKEN")
    assert token_entry["password"] is True

    manager = PluginManager()
    module = manager._load_directory_module(manifest)
    assert module.__name__ == "hermes_plugins.startup_suite_platform"
    assert callable(module.register)
    module.register(PluginContext(manifest, manager))
    entry = platform_registry.get("startup_suite")
    assert entry is not None and entry.label == "Startup Suite"
    assert entry.adapter_factory.__module__.startswith("hermes_plugins.startup_suite_platform.")
