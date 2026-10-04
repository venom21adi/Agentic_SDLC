import os
import subprocess
from pathlib import Path

import pytest

import runners
from runners import (DockerRunner, JUNIT_MARKER, LocalRunner, RunResult, parse_junit, summarize, tail,
                     default_runner, _write_files)
from workspace import UnsafePathError


def junit(passed=0, failed=0, errors=0, skipped=0, integration_passed=0, integration_failed=0):
    cases = [f'<testcase classname="tests.test_x" name="t{i}"/>' for i in range(passed)]
    cases += [f'<testcase classname="tests.test_x" name="f{i}"><failure message="boom"/></testcase>' for i in range(failed)]
    cases += [f'<testcase classname="tests.test_x" name="e{i}"><error message="err"/></testcase>' for i in range(errors)]
    cases += [f'<testcase classname="tests.test_x" name="s{i}"><skipped/></testcase>' for i in range(skipped)]
    cases += [f'<testcase classname="tests.integration.test_y" name="i{i}"/>' for i in range(integration_passed)]
    cases += [f'<testcase classname="tests.integration.test_y" name="if{i}"><failure/></testcase>' for i in range(integration_failed)]
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'


# ---- junit parsing ----

def test_parse_junit_counts_and_splits_unit_from_integration():
    c = parse_junit(junit(passed=3, failed=1, errors=1, skipped=2, integration_passed=2, integration_failed=1))
    assert c == {"unit_passed": 3, "unit_failed": 2, "integration_passed": 2, "integration_failed": 1, "skipped": 2}


@pytest.mark.parametrize("xml,expected", [
    ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>', "DTD"),
    ('<!doctype x><testsuite/>', "DTD"),
    ("<testsuite><testcase", "not valid XML"),
    ("not xml", "not valid XML"),
    ("<a>" + "x" * 1_000_001 + "</a>", "too large"),
], ids=["entity", "doctype", "truncated", "garbage", "oversized"])
def test_parse_junit_rejects_hostile_or_malformed_reports(xml, expected):
    with pytest.raises(ValueError, match=expected):
        parse_junit(xml)


# ---- pass/fail decision ----

def test_clean_run_passes():
    s = summarize(RunResult(exit_code=0, output="3 passed", junit_xml=junit(passed=3)))
    assert s.passed and s.unit_passed == 3 and s.reasons == []


def test_failures_fail_with_reason():
    s = summarize(RunResult(exit_code=1, output="1 failed", junit_xml=junit(passed=2, failed=1)))
    assert not s.passed and "1 test(s) failed" in s.reasons and "pytest exited with code 1" in s.reasons


def test_exit_zero_cannot_hide_a_failure_in_the_report():
    s = summarize(RunResult(exit_code=0, junit_xml=junit(passed=1, failed=1)))
    assert not s.passed


def test_no_tests_collected_fails():
    s = summarize(RunResult(exit_code=5, output="no tests ran", junit_xml=junit()))
    assert not s.passed and "no tests were collected" in s.reasons


def test_all_skipped_does_not_pass():
    s = summarize(RunResult(exit_code=0, junit_xml=junit(skipped=3)))
    assert not s.passed and "no tests actually ran" in s.reasons[0]


def test_exit_zero_without_report_does_not_pass():
    s = summarize(RunResult(exit_code=0, output="ok"))
    assert not s.passed and "no test report" in s.reasons[0]


def test_hostile_report_does_not_pass_even_with_exit_zero():
    s = summarize(RunResult(exit_code=0, junit_xml='<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>'))
    assert not s.passed and "unusable test report" in s.reasons[0]


def test_timeout_fails():
    s = summarize(RunResult(timed_out=True, duration_s=120, output="partial"))
    assert not s.passed and "timed out" in s.reasons[0] and s.report == "partial"


def test_report_is_truncated_from_the_front():
    assert tail("a" * 10_000).startswith("...[truncated]...") and tail("short") == "short"
    assert tail("a" * 5000 + "END").endswith("END")


# ---- docker runner (docker itself is faked: no daemon is needed or used) ----

def test_docker_command_is_locked_down():
    cmd = DockerRunner(image="img:1").build_command("n", r"C:\tmp\x")
    joined = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--pids-limit 256", "--user 65534:65534", "--memory 512m", "--memory-swap 512m", "--rm"):
        assert flag in joined, flag
    assert "type=bind,source=C:\\tmp\\x,target=/work,readonly" in joined
    assert "--privileged" not in joined and "docker.sock" not in joined and " -v " not in joined
    assert cmd[cmd.index("--workdir") + 1] == "/work" and "img:1" in cmd


class FakeDocker:
    """Stands in for subprocess.run; records calls and answers like docker would."""

    def __init__(self, image_present=True, run_result=None, build_ok=True, timeout=False):
        self.image_present, self.run_result, self.build_ok, self.timeout = image_present, run_result, build_ok, timeout
        self.calls, self.seen_files, self.host_dir = [], None, None

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "image":
            return subprocess.CompletedProcess(cmd, 0 if self.image_present else 1, "", "")
        if verb == "build":
            return subprocess.CompletedProcess(cmd, 0 if self.build_ok else 1, "", "build exploded")
        if verb == "kill":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        assert verb == "run"
        mount = next(a for a in cmd if a.startswith("type=bind"))
        self.host_dir = Path(mount.split("source=")[1].split(",target")[0])
        self.seen_files = sorted(p.relative_to(self.host_dir).as_posix() for p in self.host_dir.rglob("*") if p.is_file())
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"partial output")
        return self.run_result


def patch_docker(monkeypatch, fake):
    monkeypatch.setattr(runners.subprocess, "run", fake)
    return fake


FILES = {"app/a.py": "x = 1\n", "tests/test_a.py": "def test_a():\n    assert True\n"}


def test_docker_run_parses_marker_output_and_cleans_up(monkeypatch):
    stdout = f"..F\n1 failed\n{JUNIT_MARKER}\n{junit(passed=2, failed=1)}"
    fake = patch_docker(monkeypatch, FakeDocker(run_result=subprocess.CompletedProcess([], 1, stdout, "")))
    r = DockerRunner().run(FILES, timeout=30)
    assert r.exit_code == 1 and "1 failed" in r.output and r.junit_xml.startswith("<?xml") and r.error is None
    assert fake.seen_files == ["app/a.py", "tests/test_a.py"]       # files were on disk when the container ran
    assert not fake.host_dir.exists()                               # and removed afterwards


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_level_failures_are_infrastructure_errors_not_test_failures(monkeypatch, code):
    patch_docker(monkeypatch, FakeDocker(run_result=subprocess.CompletedProcess([], code, "", "daemon not running")))
    r = DockerRunner().run(FILES, timeout=30)
    assert r.error and "daemon not running" in r.error and r.exit_code is None


def test_container_exit_137_is_a_test_failure_not_infrastructure(monkeypatch):
    patch_docker(monkeypatch, FakeDocker(run_result=subprocess.CompletedProcess([], 137, "Killed", "")))
    r = DockerRunner().run(FILES, timeout=30)
    assert r.error is None and r.exit_code == 137 and not summarize(r).passed


def test_docker_timeout_kills_the_container(monkeypatch):
    fake = patch_docker(monkeypatch, FakeDocker(timeout=True))
    r = DockerRunner().run(FILES, timeout=5)
    assert r.timed_out and r.output == "partial output"
    assert any(c[1] == "kill" for c in fake.calls)
    assert not fake.host_dir.exists()


def test_missing_image_is_built_once_and_build_failure_is_reported(monkeypatch):
    ok = patch_docker(monkeypatch, FakeDocker(image_present=False, run_result=subprocess.CompletedProcess([], 0, f"{JUNIT_MARKER}\n{junit(passed=1)}", "")))
    runner = DockerRunner()
    runner.run(FILES, 30)
    runner.run(FILES, 30)
    assert sum(1 for c in ok.calls if c[1] == "build") == 1
    patch_docker(monkeypatch, FakeDocker(image_present=False, build_ok=False))
    r = DockerRunner().run(FILES, 30)
    assert r.error and "build exploded" in r.error


def test_docker_binary_missing_is_infrastructure_error():
    r = DockerRunner(docker="definitely-not-a-real-docker-binary").run(FILES, 5)
    assert r.error and "docker unavailable" in r.error


def test_unsafe_paths_never_reach_the_sandbox_directory(tmp_path):
    with pytest.raises(UnsafePathError):
        _write_files(tmp_path, {"../escape.py": "x"})
    assert not (tmp_path.parent / "escape.py").exists()


# ---- local runner (real pytest, fixed trusted test code) ----

def test_local_runner_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv("ALLOW_UNSAFE_LOCAL_RUNNER", raising=False)
    with pytest.raises(PermissionError, match="without isolation"):
        LocalRunner()
    monkeypatch.setenv("ALLOW_UNSAFE_LOCAL_RUNNER", "1")
    LocalRunner()  # allowed once explicitly enabled


def test_default_runner_selection(monkeypatch):
    monkeypatch.delenv("ALLOW_UNSAFE_LOCAL_RUNNER", raising=False)
    assert isinstance(default_runner("docker", "img"), DockerRunner)
    with pytest.raises(PermissionError):
        default_runner("local", "img")
    with pytest.raises(ValueError):
        default_runner("bogus", "img")


LOCAL = LocalRunner(unsafe_ok=True)
GOOD = {"app/auth.py": "def login(u, p):\n    return bool(u and p)\n",
        "tests/test_auth.py": "from app.auth import login\n\ndef test_ok():\n    assert login('a', 'b')\n\ndef test_empty():\n    assert not login('', 'b')\n"}


def test_local_runner_real_pass():
    s = summarize(LOCAL.run(GOOD, timeout=60))
    assert s.passed and s.unit_passed == 2, s.reasons


def test_local_runner_real_failure_reports_the_assertion():
    bad = {**GOOD, "app/auth.py": "def login(u, p):\n    return False\n"}
    s = summarize(LOCAL.run(bad, timeout=60))
    assert not s.passed and s.unit_failed == 1 and s.unit_passed == 1
    assert "assert" in s.report and "test_auth.py" in s.report


def test_local_runner_import_error_is_a_failure():
    s = summarize(LOCAL.run({"tests/test_x.py": "import does_not_exist\n\ndef test_a():\n    pass\n"}, timeout=60))
    assert not s.passed


def test_local_runner_collects_nothing_when_there_are_no_tests():
    s = summarize(LOCAL.run({"app/a.py": "x = 1\n"}, timeout=60))
    assert not s.passed and "no tests were collected" in s.reasons


def test_local_runner_times_out_runaway_tests():
    r = LOCAL.run({"tests/test_loop.py": "def test_forever():\n    while True:\n        pass\n"}, timeout=3)
    assert r.timed_out and not summarize(r).passed


def test_local_runner_hides_the_parent_environment(monkeypatch):
    monkeypatch.setenv("SDLC_TEST_SECRET", "hunter2")
    code = "import os\n\ndef test_env():\n    assert os.environ.get('SDLC_TEST_SECRET') is None\n"
    assert summarize(LOCAL.run({"tests/test_env.py": code}, timeout=60)).passed


def test_local_runner_cleans_up_its_temp_dirs():
    import tempfile
    before = {p.name for p in Path(tempfile.gettempdir()).glob("sdlc-local-*")}
    LOCAL.run(GOOD, timeout=60)
    assert {p.name for p in Path(tempfile.gettempdir()).glob("sdlc-local-*")} == before
