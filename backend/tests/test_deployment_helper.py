"""The deployment helper must preserve the running service on build failure."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX deployment helper")
HELPER = Path(__file__).resolve().parents[2] / "tools" / "sentinel-ctl"


def run_helper(tmp_path: Path, *args: str, dirty=False, build_fails=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands"
    stubs = {
        "git": 'echo "git $*" >> "$TEST_LOG"\n'
        'if [ "$1" = status ] && [ "$TEST_DIRTY" = 1 ]; then echo " M user-file"; fi',
        "docker": 'echo "docker $*" >> "$TEST_LOG"\n'
        'if [ "$1" = inspect ]; then echo sha256:running; fi\n'
        'case "$*" in *" build sentinel") [ "$TEST_FAIL" != 1 ] || exit 2;; esac',
        "curl": 'echo "curl $*" >> "$TEST_LOG"\necho healthy',
    }
    for name, body in stubs.items():
        target = bin_dir / name
        target.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        target.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "SENTINEL_APP_DIR": str(tmp_path),
        "TEST_LOG": str(log),
        "TEST_DIRTY": str(int(dirty)),
        "TEST_FAIL": str(int(build_fails)),
    }
    result = subprocess.run(  # noqa: S603
        [shutil.which("sh") or "/bin/sh", str(HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, log.read_text() if log.exists() else ""


def test_dirty_update_does_not_pull_or_touch_service(tmp_path):
    result, log = run_helper(tmp_path, "update", dirty=True)
    assert result.returncode == 1
    assert "pull" not in log and "docker" not in log


def test_build_failure_keeps_running_image_and_container(tmp_path):
    result, log = run_helper(tmp_path, "rebuild", build_fails=True)
    assert result.returncode == 2
    assert "image tag sha256:running sentinel-cctv:before-" in log
    assert " up " not in log and " stop" not in log


def test_success_retains_image_then_builds_and_checks_health(tmp_path):
    result, log = run_helper(tmp_path, "rebuild")
    assert result.returncode == 0
    assert log.index("image tag") < log.index("build sentinel") < log.index("up -d")
    assert "up -d --no-build sentinel" in log
    assert "system/health" in log


def test_rollback_rejects_arbitrary_image(tmp_path):
    result, log = run_helper(tmp_path, "rollback", "unknown:latest")
    assert result.returncode == 1 and not log


def test_rollback_uses_local_image_without_git_or_schema_reset(tmp_path):
    result, log = run_helper(tmp_path, "rollback", "sentinel-cctv:before-test")
    assert result.returncode == 0
    assert "image tag sentinel-cctv:before-test sentinel-cctv:local" in log
    assert "git " not in log and "pull" not in log and "alembic" not in log
