import subprocess
import sys
from pathlib import Path

from jarvis.tools.git import GitInspector


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / " lead.txt").write_text("leading space\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_ls_files_default_mode_strips_output(tmp_path):
    repo = tmp_path / "default-repo"
    _init_repo(repo)

    result = GitInspector._run(["ls-files", "-z"], repo)

    assert result.startswith("lead.txt")


def test_ls_files_raw_mode_preserves_exact_output(tmp_path):
    repo = tmp_path / "raw-repo"
    _init_repo(repo)

    result = GitInspector._run(["ls-files", "-z"], repo, raw=True)

    assert result.startswith(" lead.txt")
    assert result.endswith("\0")


def test_raw_mode_keeps_errors_as_error_strings(tmp_path):
    repo = tmp_path / "error-repo"
    _init_repo(repo)

    result = GitInspector._run(["rev-parse", "--verify", "nope"], repo, raw=True)

    assert result.startswith("ERROR:")


def test_raw_mode_keeps_git_hardening(tmp_path):
    repo = tmp_path / "fsmonitor-repo"
    marker = tmp_path / "fsmonitor-ran"
    _init_repo(repo)
    executable = Path(sys.executable).as_posix()
    marker_path = marker.as_posix()
    code = f"from pathlib import Path; Path({marker_path!r}).touch()"
    command = f'"{executable}" -c "{code}"'
    subprocess.run(["git", "config", "core.fsmonitor", command], cwd=repo, check=True)

    result = GitInspector._run(["status", "--short"], repo, raw=True)

    assert not result.startswith("ERROR:")
    assert not marker.exists()
