"""Installed setup pages follow the actual view registry and file readers."""
import json

import pytest

from raven import setup_content
from raven.command_registry import BY_NAME
from raven.config import Config
from raven.controller import App
from raven.metrics import MetricReader
from raven.model import Store
from raven.research import RESEARCH_VIEWS
from raven.slurm import parse_gpu_trace
from raven.views import ANALYTICS_VIEWS


def test_setup_covers_every_registered_view_and_uses_registered_actions():
    keys = set(setup_content.topic_keys())
    assert {key[9:] for key in keys if key.startswith("research.")} == dict(RESEARCH_VIEWS).keys()
    assert {key[10:] for key in keys if key.startswith("analytics.")} == dict(ANALYTICS_VIEWS).keys()
    for key in keys:
        page = setup_content.topic(key)
        assert page["title"] and page["lines"] and page["actions"]
        for label, command, prompt in page["actions"]:
            assert label and command.split()[0] in BY_NAME
            assert type(prompt) is bool
            if not prompt:
                assert command == command.strip()


@pytest.mark.parametrize("section,view", [
    *(('research', key) for key, _ in RESEARCH_VIEWS),
    *(('analytics', key) for key, _ in ANALYTICS_VIEWS),
])
def test_open_view_action_reaches_the_advertised_workspace(section, view):
    app = App(Store(persist=False), None, None, Config(), "test", ascii_=True)
    _, command, prompt = setup_content.topic(section + "." + view)["actions"][0]
    assert not prompt
    app.run_command(command)
    assert app.command_ok
    assert app.tab == section
    assert getattr(app, section + "_view") == view


def test_visible_metric_and_gpu_examples_are_consumed_by_real_readers(tmp_path):
    metric_page = setup_content.topic("research.experiment")
    row = next(line for line in metric_page["lines"] if line.startswith('{"t":'))
    path = tmp_path / "metrics.jsonl"
    path.write_text(row + "\n", encoding="utf-8")
    result = MetricReader().read(path)
    assert result["status"] == "ok"
    assert result["latest"] == {"loss": 0.42}
    assert result["progress"]["fraction"] == 0.01
    assert not result["errors"]
    gpu_page = setup_content.topic("analytics.job")
    row = next(line for line in gpu_page["lines"] if line.startswith("2026/"))
    samples = parse_gpu_trace((row + "\n").encode())
    assert len(samples) == 1
    assert samples[0]["index"] == 0
    assert samples[0]["util"] == 83
    assert samples[0]["mem"] == 6120


def test_visible_manifest_and_output_contract_use_native_validation(tmp_path):
    from raven.artifacts import validate_contract
    from raven.projects import validate_inventory

    lines = setup_content.topic("project")["lines"]
    inventory = json.loads(next(line.split(": ", 1)[1] for line in lines
                                if line.startswith("Minimal standalone manifest:")))
    assert validate_inventory(inventory)["run_id"] == "fit-a1"
    lines = setup_content.topic("research.artifacts")["lines"]
    contract = json.loads(next(line.split(": ", 1)[1] for line in lines
                               if line.startswith("Minimal output contract:")))
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/results.csv").write_text("step,loss\n1,0.42\n")
    assert validate_contract(contract, tmp_path)["valid"]


def test_topic_lookup_needs_no_checkout_or_process_and_returns_independent_pages(monkeypatch):
    import builtins
    import subprocess

    def blocked(*args, **kwargs):
        pytest.fail("opening a setup page performed I/O")

    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    for key in setup_content.topic_keys():
        page = setup_content.topic(key)
        page["lines"].clear()
        page["actions"].clear()
        assert setup_content.topic(key)["lines"]
        assert setup_content.topic(key)["actions"]
    with pytest.raises(ValueError, match="Unknown setup topic"):
        setup_content.topic("research.unknown")
