"""Public launch/CLI behavior used in the research guide."""
from raven import cli
from raven.config import Config


def test_research_launch_opens_requested_view_without_sampling():
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--tab", "research", "--research-view", "arrays"]), Config())
    try:
        assert session.app.tab == "research"
        assert session.app.research_view == "arrays"
        assert not session.backend.calls
    finally:
        session.close()


def test_unambiguous_offline_prepare_abbreviation_never_starts_sampler(tmp_path, monkeypatch, capsys):
    script = tmp_path / "a.sbatch"
    script.write_text("#!/bin/bash\necho example\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    def forbidden(*args, **kwargs):
        raise AssertionError("offline preparation must not initialize Slurm")
    monkeypatch.setattr(cli, "build", forbidden)
    assert cli.main(["--config", str(config), "run", "prep", str(script), "--workdir", str(tmp_path)]) == 0
    assert '"valid": true' in capsys.readouterr().out
