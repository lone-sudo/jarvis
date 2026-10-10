"""Read-only setup checks for ``jarvis doctor``."""

from types import SimpleNamespace

import pytest

from jarvis.interface import doctor
from jarvis.interface.cli import main


def test_python_requirement_passes_and_fails(monkeypatch):
    monkeypatch.setattr(doctor, "_requires_python", lambda: ">=3.10")
    monkeypatch.setattr(doctor.sys, "version_info", (3, 11, 0))
    assert doctor.check_python().status == "OK"

    monkeypatch.setattr(doctor.sys, "version_info", (3, 9, 9))
    result = doctor.check_python()
    assert result.status == "FAIL"
    assert "does not satisfy" in result.detail


def test_python_requirement_read_error_fails(monkeypatch):
    def unavailable():
        raise OSError("missing metadata")

    monkeypatch.setattr(doctor, "_requires_python", unavailable)
    assert doctor.check_python().status == "FAIL"


def test_pip_available_and_missing(monkeypatch):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="pip 25.0", stderr=""),
    )
    assert doctor.check_pip().status == "OK"

    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="No module named pip"),
    )
    result = doctor.check_pip()
    assert result.status == "WARN"
    assert "No module named pip" in result.detail


def test_pip_subprocess_error_is_warning(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("python unavailable")

    monkeypatch.setattr(doctor.subprocess, "run", fail)
    assert doctor.check_pip().status == "WARN"


def test_git_found_and_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "git")
    assert doctor.check_git().status == "OK"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.check_git().status == "FAIL"


def test_jarvis_import_passes_and_fails(monkeypatch):
    monkeypatch.setattr(doctor.importlib, "import_module", lambda name: object())
    assert doctor.check_jarvis_import().status == "OK"

    def fail(name):
        raise ImportError("module missing")

    monkeypatch.setattr(doctor.importlib, "import_module", fail)
    assert doctor.check_jarvis_import().status == "FAIL"


def test_console_command_not_installed_is_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.check_console_command().status == "OK"


def test_console_command_runs_help_without_pythonpath(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "jarvis")
    monkeypatch.setenv("PYTHONPATH", "should-not-reach-command")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="usage: jarvis", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    result = doctor.check_console_command()

    assert result.status == "OK"
    assert calls[0][0] == ["jarvis", "--help"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 10
    assert "PYTHONPATH" not in calls[0][1]["env"]
    assert "shell" not in calls[0][1]
    assert doctor.os.environ["PYTHONPATH"] == "should-not-reach-command"


def test_console_command_nonzero_exit_warns_with_error_and_fix(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "jarvis")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr='Traceback (most recent call last):\n  File "jarvis", line 1\nModuleNotFoundError: No module named \'jarvis\'\n',
        ),
    )
    result = doctor.check_console_command()
    assert result.status == "WARN"
    assert "ModuleNotFoundError: No module named 'jarvis'" in result.detail
    assert '$env:PYTHONPATH = "src"; python -m jarvis.interface.cli' in result.detail


def test_console_command_timeout_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "jarvis")

    def timeout(args, **kwargs):
        raise doctor.subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="launch stalled")

    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    result = doctor.check_console_command()
    assert result.status == "WARN"
    assert "launch stalled" in result.detail
    assert '$env:PYTHONPATH = "src"; python -m jarvis.interface.cli' in result.detail


def test_console_command_execution_error_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "jarvis")

    def fail(args, **kwargs):
        raise OSError("executable could not start")

    monkeypatch.setattr(doctor.subprocess, "run", fail)
    result = doctor.check_console_command()
    assert result.status == "WARN"
    assert "executable could not start" in result.detail
    assert '$env:PYTHONPATH = "src"; python -m jarvis.interface.cli' in result.detail


def test_workspace_exists_writable_and_missing(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(doctor, "_workspace_path", lambda: workspace)
    monkeypatch.setattr(doctor.os, "access", lambda path, mode: True)
    assert doctor.check_workspace().status == "OK"

    monkeypatch.setattr(doctor, "_workspace_path", lambda: tmp_path / "missing")
    assert doctor.check_workspace().status == "FAIL"


def test_workspace_not_directory_and_not_writable(monkeypatch, tmp_path):
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(doctor, "_workspace_path", lambda: file_path)
    assert doctor.check_workspace().status == "FAIL"

    folder = tmp_path / "readonly"
    folder.mkdir()
    monkeypatch.setattr(doctor, "_workspace_path", lambda: folder)
    monkeypatch.setattr(doctor.os, "access", lambda path, mode: False)
    assert doctor.check_workspace().status == "FAIL"


def test_workspace_resolution_error_fails(monkeypatch):
    class BadPath:
        def resolve(self, strict=False):
            raise OSError("cannot resolve")

    monkeypatch.setattr(doctor, "_workspace_path", lambda: BadPath())
    assert doctor.check_workspace().status == "FAIL"


def test_database_path_resolves_without_creating_files(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(doctor, "_workspace_path", lambda: workspace)
    result = doctor.check_database_path()
    assert result.status == "OK"
    assert str(tmp_path / ".jarvis" / "jarvis_state.db") in result.detail
    assert not (tmp_path / ".jarvis").exists()


def test_database_path_resolution_error_fails(monkeypatch):
    class BadPath:
        def resolve(self, strict=False):
            raise OSError("cannot resolve")

    monkeypatch.setattr(doctor, "_workspace_path", lambda: BadPath())
    assert doctor.check_database_path().status == "FAIL"


def test_doctor_command_reports_checks_and_exits_only_on_fail(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [doctor.Check("pip", "WARN", "missing"), doctor.Check("git", "OK", "found")],
    )
    monkeypatch.setattr("sys.argv", ["jarvis", "doctor"])
    main()
    output = capsys.readouterr().out
    assert "WARN: pip" in output
    assert "OK: git" in output

    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [doctor.Check("git", "FAIL", "missing")],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_doctor_command_is_read_only_for_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="pip missing"),
    )
    checks = doctor.run_checks()
    assert checks
    assert not workspace.exists()
