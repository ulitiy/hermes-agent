"""Real collection and CRUD must leave an artificial operator cron store untouched."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("preset_home", [False, True])
@pytest.mark.parametrize("collect_only", [False, True])
def test_cron_collection_and_crud_preserve_operator_store(tmp_path, preset_home, collect_only):
    repo = Path(__file__).resolve().parents[1]
    operator = tmp_path / "operator"
    store = operator / ".hermes" / "cron" / "jobs.json"
    store.parent.mkdir(parents=True)
    sentinel = '{"jobs": [], "version": 1}\n'
    store.write_text(sentinel)
    env = {
        "HOME": str(operator), "USERPROFILE": str(operator),
        "LOCALAPPDATA": str(operator / "AppData"),
        "PATH": os.environ.get("PATH", ""), "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if preset_home:
        env["HERMES_HOME"] = str(store.parent.parent)
    args = ["-o", "addopts=", "-q", "tests/cron/test_ticker_stall.py"]
    if collect_only:
        args.append("--collect-only")
    code = (
        "import sys, pytest; from pathlib import Path; "
        "rc = pytest.main(sys.argv[2:]); "
        "import cron.jobs as jobs; "
        "assert jobs.JOBS_FILE.resolve() != Path(sys.argv[1]).resolve(), "
        "'collection retained the inherited cron store'; sys.exit(rc)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(store), *args],
        cwd=repo, env=env, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert store.read_text() == sentinel
