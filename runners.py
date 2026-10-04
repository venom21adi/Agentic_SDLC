"""Execute generated tests and report structured results.

DockerRunner is the real runner: generated code runs in a locked-down container (no network, read-only
filesystem, dropped capabilities, CPU/memory/pid/time limits). LocalRunner runs it directly on this
machine; it exists for development and refuses to run unless explicitly enabled.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Protocol

from workspace import normalize_paths

JUNIT_MARKER = "===JUNIT==="
MAX_JUNIT_BYTES = 1_000_000
MAX_REPORT_CHARS = 6000
PYTEST_ARGS = ["-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
# Exit codes docker itself uses when it cannot run the container; these are infrastructure errors,
# not test outcomes.
DOCKER_ERROR_CODES = {125, 126, 127}


@dataclass
class RunResult:
    exit_code: Optional[int] = None
    output: str = ""
    junit_xml: Optional[str] = None
    timed_out: bool = False
    duration_s: float = 0.0
    error: Optional[str] = None  # infrastructure failure (runner could not run the tests at all)


@dataclass
class Summary:
    passed: bool
    unit_passed: int = 0
    unit_failed: int = 0
    integration_passed: int = 0
    integration_failed: int = 0
    skipped: int = 0
    report: str = ""
    reasons: list[str] = field(default_factory=list)  # why it did not pass

    @property
    def total_run(self) -> int:
        return self.unit_passed + self.unit_failed + self.integration_passed + self.integration_failed


class TestRunner(Protocol):
    __test__ = False

    def run(self, files: Mapping[str, str], timeout: int) -> RunResult: ...


def parse_junit(xml_text: str) -> dict[str, int]:
    """Count outcomes from pytest's JUnit XML. Raises ValueError on anything suspicious or malformed."""
    if len(xml_text.encode("utf-8", errors="replace")) > MAX_JUNIT_BYTES:
        raise ValueError("junit report too large")
    # The report is written inside the sandbox by code we do not trust: refuse entity tricks outright.
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise ValueError("junit report contains a DTD")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"junit report is not valid XML: {e}") from e

    counts = {"unit_passed": 0, "unit_failed": 0, "integration_passed": 0, "integration_failed": 0, "skipped": 0}
    cases = list(root.iter("testcase"))
    for case in cases:
        kind = "integration" if "integration" in (case.get("classname", "") + case.get("name", "")).lower() else "unit"
        tags = {child.tag for child in case}
        if "skipped" in tags:
            counts["skipped"] += 1
        elif tags & {"failure", "error"}:
            counts[f"{kind}_failed"] += 1
        else:
            counts[f"{kind}_passed"] += 1
    return counts


def tail(text: str, limit: int = MAX_REPORT_CHARS) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


def summarize(run: RunResult) -> Summary:
    """
    Decide pass/fail from a RunResult. A run passes only if pytest exited 0, nothing failed, and at least
    one test actually executed (so an empty or fully-skipped suite cannot pass).
    """
    report = tail(run.output)
    if run.timed_out:
        return Summary(False, report=report, reasons=[f"timed out after {run.duration_s:.0f}s"])

    counts = {"unit_passed": 0, "unit_failed": 0, "integration_passed": 0, "integration_failed": 0, "skipped": 0}
    reasons: list[str] = []
    if run.junit_xml:
        try:
            counts = parse_junit(run.junit_xml)
        except ValueError as e:
            reasons.append(f"unusable test report: {e}")
    elif run.exit_code == 0:
        reasons.append("pytest produced no test report")

    s = Summary(False, report=report, **counts)
    if run.exit_code == 5:
        reasons.append("no tests were collected")
    elif run.exit_code not in (0, None):
        reasons.append(f"pytest exited with code {run.exit_code}")
    if s.unit_failed or s.integration_failed:
        reasons.append(f"{s.unit_failed + s.integration_failed} test(s) failed")
    if run.exit_code == 0 and s.total_run == 0 and not reasons:
        reasons.append("no tests actually ran (all skipped?)")
    s.reasons = reasons
    s.passed = run.exit_code == 0 and not reasons
    return s


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    paths = normalize_paths(files.keys())  # re-validated here even though callers already did
    base = root.resolve()
    for rel, content in zip(paths, files.values()):
        target = (root / rel).resolve()
        if base not in target.parents:
            raise ValueError(f"path escapes sandbox: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


class DockerRunner:
    """Runs pytest inside a locked-down container built from sandbox/Dockerfile."""

    def __init__(self, image: str = "agentic-sdlc-sandbox:latest", memory: str = "512m", cpus: str = "1",
                 pids: int = 256, docker: str = "docker"):
        self.image, self.memory, self.cpus, self.pids, self.docker = image, memory, cpus, pids, docker
        self._image_ready = False

    def build_command(self, name: str, host_dir: str) -> list[str]:
        script = (
            "python " + " ".join(PYTEST_ARGS) + " --junitxml=/tmp/report.xml; code=$?; "
            f"echo; echo {JUNIT_MARKER}; cat /tmp/report.xml 2>/dev/null; exit $code"
        )
        return [
            self.docker, "run", "--rm", "--name", name,
            "--network", "none",
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", self.cpus, "--pids-limit", str(self.pids),
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,source={host_dir},target=/work,readonly",
            "--workdir", "/work",
            self.image, "sh", "-c", script,
        ]

    def _ensure_image(self) -> Optional[str]:
        """Return an error string if the sandbox image is unavailable and cannot be built."""
        if self._image_ready:
            return None
        try:
            if subprocess.run([self.docker, "image", "inspect", self.image], capture_output=True, timeout=30).returncode != 0:
                ctx = Path(__file__).resolve().parent / "sandbox"
                built = subprocess.run([self.docker, "build", "-t", self.image, str(ctx)],
                                       capture_output=True, text=True, timeout=900)
                if built.returncode != 0:
                    return f"could not build sandbox image: {built.stderr.strip()[-500:]}"
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"docker unavailable: {e}"
        self._image_ready = True
        return None

    def run(self, files: Mapping[str, str], timeout: int) -> RunResult:
        if (err := self._ensure_image()) is not None:
            return RunResult(error=err)
        host_dir = tempfile.mkdtemp(prefix="sdlc-sandbox-")
        name = f"sdlc-test-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        try:
            _write_files(Path(host_dir), files)
            try:
                proc = subprocess.run(
                    self.build_command(name, host_dir), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout,
                )
            except subprocess.TimeoutExpired as e:
                subprocess.run([self.docker, "kill", name], capture_output=True, timeout=30)
                out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
                return RunResult(output=out, timed_out=True, duration_s=time.monotonic() - start)
            except OSError as e:
                return RunResult(error=f"docker unavailable: {e}")

            if proc.returncode in DOCKER_ERROR_CODES:
                return RunResult(error=f"docker failed to run the container (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
            output, _, junit = proc.stdout.partition(JUNIT_MARKER + "\n")
            return RunResult(
                exit_code=proc.returncode, output=output + proc.stderr, junit_xml=junit.strip() or None,
                duration_s=time.monotonic() - start,
            )
        finally:
            shutil.rmtree(host_dir, ignore_errors=True)


class LocalRunner:
    """
    Runs pytest directly on THIS machine with a scrubbed environment. It executes untrusted generated
    code with your user's permissions and is not a sandbox. Development only.
    """

    def __init__(self, unsafe_ok: bool = False):
        if not (unsafe_ok or os.getenv("ALLOW_UNSAFE_LOCAL_RUNNER") == "1"):
            raise PermissionError(
                "LocalRunner executes generated code on this machine without isolation. "
                "Set ALLOW_UNSAFE_LOCAL_RUNNER=1 to allow it, or use DockerRunner."
            )

    def run(self, files: Mapping[str, str], timeout: int) -> RunResult:
        work = tempfile.mkdtemp(prefix="sdlc-local-")
        report = Path(work + "-report.xml")
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        start = time.monotonic()
        try:
            _write_files(Path(work), files)
            try:
                proc = subprocess.run(
                    [sys.executable, *PYTEST_ARGS, f"--junitxml={report}"], cwd=work, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                )
            except subprocess.TimeoutExpired as e:
                out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
                return RunResult(output=out, timed_out=True, duration_s=time.monotonic() - start)
            junit = report.read_text(encoding="utf-8", errors="replace") if report.exists() else None
            return RunResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr, junit_xml=junit,
                             duration_s=time.monotonic() - start)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            report.unlink(missing_ok=True)


def default_runner(kind: str, image: str) -> "TestRunner":
    if kind == "docker":
        return DockerRunner(image)
    if kind == "local":
        return LocalRunner()
    raise ValueError(f"unknown TEST_RUNNER {kind!r} (expected 'docker' or 'local')")
