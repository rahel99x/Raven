"""The generated config must make its documented editable settings effective."""
import pytest

from raven.config import Config, DEFAULTS, load_file


def test_generated_toml_keeps_root_settings_outside_tables(tmp_path):
    pytest.importorskip("tomllib")
    path = tmp_path / "config.toml"
    Config.write_default(str(path))
    data = load_file(str(path))
    for key in ("weather", "budget", "gpu_types", "series_keep", "analytics_days", "theme"):
        assert data[key] == DEFAULTS[key]
    assert set(data["notify"]) == set(DEFAULTS["notify"])


def test_editing_documented_root_settings_changes_loaded_configuration(tmp_path):
    pytest.importorskip("tomllib")
    path = tmp_path / "config.toml"
    Config.write_default(str(path))
    text = path.read_text()
    text = text.replace('theme = "default"', 'theme = "reader"')
    text = text.replace("weather = true", "weather = false")
    text = text.replace("budget = true", "budget = false")
    text = text.replace('# host = "login.example.edu"', 'host = "login.example.edu"', 1)
    text = text.replace('# ssh_user = "me"', 'ssh_user = "me"', 1)
    text = text.replace('# ssh_opts = ["-J", "bastion"]', 'ssh_opts = ["-J", "bastion"]', 1)
    text = text.replace('# plugins = ["~/my-raven-plugins"]', 'plugins = ["~/my-raven-plugins"]', 1)
    path.write_text(text)
    config = Config.load(str(path))
    assert config["theme"] == "reader"
    assert not config["weather"] and not config["budget"]
    assert config["host"] == "login.example.edu"
    assert config["ssh_user"] == "me"
    assert config["ssh_opts"] == ["-J", "bastion"]
    assert config["plugins"] == ["~/my-raven-plugins"]
