"""The default cron fixture scopes paths per test, including imported CRUD."""
import os
from pathlib import Path

import cron.jobs as jobs


def test_default_store_is_inside_current_test_home():
    home = Path(os.environ["HERMES_HOME"]).resolve()
    job = jobs.create_job(prompt="test-only", schedule="0 7 * * *")
    assert jobs.JOBS_FILE.is_relative_to(home)
    assert jobs.get_job(job["id"])["prompt"] == "test-only"


def test_next_test_starts_with_empty_store():
    assert jobs.load_jobs() == []
