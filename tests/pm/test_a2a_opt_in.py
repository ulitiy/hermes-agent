"""Use the real manifest and PM command builder, without installing packages."""
from pathlib import Path
from subprocess import CompletedProcess

from pm.environment import PythonEnvironment
from pm.features import opt_in_extras


def test_a2a_is_excluded_from_bundles_but_explicitly_installable(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    calls = []

    def run(self, args, **kwargs):
        calls.append(args)
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(PythonEnvironment, "_run", run)
    environment = PythonEnvironment(uv=tmp_path / "uv", python=tmp_path / "python",
                                    destination=tmp_path / "venv", cache=tmp_path / "cache", env={})
    environment.sync(root, all_extras=True)
    environment.sync(root, extras=["a2a"])
    assert "a2a" in opt_in_extras(root)
    assert ["--no-extra", "a2a"] == calls[0][calls[0].index("--no-extra"):][:2]
    assert "--all-extras" not in calls[1]
    assert calls[1][-2:] == ["--extra", "a2a"]
