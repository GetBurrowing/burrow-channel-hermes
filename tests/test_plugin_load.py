"""Load this repository as a Hermes directory plugin with Hermes's OWN
manifest parser, loader and PluginContext (hermes_cli/plugins_manifest.py
``parse_manifest_file``, hermes_cli/plugins_loader.py
``_load_directory_module``), then register through the real
``PluginContext.register_platform``. This is what keeps the ``__package__``
guard in the root ``__init__.py`` honest: an entry point broken under Hermes
fails here even though pytest never sees it."""

from pathlib import Path

import yaml

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
    # The token itself is not a required env setting (README "Security notes").
    assert names == ["SUITE_URL", "SUITE_RUNTIME_ID", "SUITE_RUNTIME_TOKEN_FILE"]
    raw = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    legacy = next(e for e in raw["optional_env"] if e["name"] == "SUITE_RUNTIME_TOKEN")
    assert legacy["password"] is True and legacy["secret"] is True

    manager = PluginManager()
    module = manager._load_directory_module(manifest)
    assert module.__name__ == "hermes_plugins.startup_suite_platform"
    assert callable(module.register)
    module.register(PluginContext(manifest, manager))
    entry = platform_registry.get("startup_suite")
    assert entry is not None and entry.label == "Startup Suite"
    assert entry.adapter_factory.__module__.startswith("hermes_plugins.startup_suite_platform.")


def test_install_prompt_masks_the_token(monkeypatch, tmp_path):
    """`hermes plugins install` prompts for every unset requires_env entry and
    masks only entries with `secret: true` (plugins_cmd_install.py
    _prompt_plugin_env_vars); `password` does not reach that prompt. Drives
    Hermes's own prompt function and records which reader each name got."""
    from hermes_cli import config as hermes_config
    from hermes_cli import plugins_cmd, plugins_cmd_install

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("SUITE_URL", "SUITE_RUNTIME_ID", "SUITE_RUNTIME_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    readers: dict[str, str] = {}

    def reader(kind):
        def read(prompt):
            readers[prompt.strip().rstrip(":")] = kind
            return ""

        return read

    monkeypatch.setattr(plugins_cmd, "masked_secret_prompt", reader("masked"))
    monkeypatch.setattr(plugins_cmd_install, "line_input", reader("echoed"))
    monkeypatch.setattr(hermes_config, "save_env_value", lambda *a: None)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())

    class Console:
        def print(self, *a, **k):
            pass

    plugins_cmd_install._prompt_plugin_env_vars(manifest, Console())
    # No required entry is a secret any more, so none is prompted for as one;
    # the token never reaches this prompt (install.sh writes the file first).
    assert readers == {
        "SUITE_URL": "echoed",
        "SUITE_RUNTIME_ID": "echoed",
        "SUITE_RUNTIME_TOKEN_FILE": "echoed",
    }
