"""The verification-artifact exception must not authorize broader deletion."""

from unittest.mock import patch

import pytest

from tools.approval import detect_dangerous_command


@pytest.mark.platforms("linux")
def test_nonrecursive_verification_artifact_cleanup_is_not_dangerous():
    with patch("tempfile.gettempdir", return_value="/tmp"):
        for prefix in ("hermes-verify-", "hermes-ad-hoc-"):
            assert detect_dangerous_command(f"rm -f /tmp/{prefix}example.py") == (
                False, None, None,
            )


@pytest.mark.require_symlinks
def test_symlinked_temp_dir_only_exempts_canonical_target(tmp_path):
    real_temp = tmp_path / "real-temp"
    real_temp.mkdir()
    linked_temp = tmp_path / "linked-temp"
    linked_temp.symlink_to(real_temp, target_is_directory=True)
    basename = "hermes-verify-example.py"
    with patch("tempfile.gettempdir", return_value=str(linked_temp)):
        assert detect_dangerous_command(f"rm -f {linked_temp / basename}")[0] is True
        assert detect_dangerous_command(f"rm -f {real_temp / basename}") == (
            False, None, None,
        )


def test_verification_cleanup_exemption_rejects_broader_deletions():
    commands = (
        "rm -rf /tmp/hermes-verify-example.py",
        "rm -f /tmp/hermes-verify-example.py /tmp/other.py",
        "rm -f /tmp/nested/../hermes-verify-example.py",
        "rm -f /tmp/a/../../tmp/hermes-verify-example.py",
        "rm -f /var/tmp/hermes-verify-example.py",
        "rm -f /tmp/hermes-verify-*",
        "rm -f /tmp/hermes-verify-$(touch>/tmp/pwned).py",
        "rm -f /tmp/hermes-ad-hoc-`touch>/tmp/pwned`.py",
        "rm -f /tmp/hermes-verify-example.py; touch /tmp/pwned",
    )
    with patch("tempfile.gettempdir", return_value="/tmp"):
        for command in commands:
            dangerous, key, description = detect_dangerous_command(command)
            assert dangerous is True, command
            assert key is not None, command
            assert "delete" in description.lower(), command
