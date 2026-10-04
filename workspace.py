"""Git-backed workspace where implementation agents commit generated code, one branch per ticket.

Each commit happens in a throwaway git worktree, so branches stay isolated from each other and the
main checkout is never modified. Merging is a human step and is not done here.
"""
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

GIT_TIMEOUT = 60
_IDENTITY = ["-c", "user.name=agentic-sdlc", "-c", "user.email=agent@agentic-sdlc.local",
             "-c", "commit.gpgsign=false"]


class WorkspaceError(RuntimeError):
    pass


class UnsafePathError(ValueError):
    pass


def safe_relpath(raw: object) -> str:
    """Validate an agent-supplied file path and return it normalized (posix, relative)."""
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafePathError(f"invalid path {raw!r}")
    p = raw.strip().replace("\\", "/")
    if "\0" in p:
        raise UnsafePathError(f"NUL in path {raw!r}")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise UnsafePathError(f"absolute path not allowed: {raw!r}")
    parts = [part for part in PurePosixPath(p).parts if part != "."]
    if not parts:
        raise UnsafePathError(f"empty path {raw!r}")
    for part in parts:
        if part == "..":
            raise UnsafePathError(f"path escapes workspace: {raw!r}")
        if ":" in part:  # drive letters and NTFS alternate data streams
            raise UnsafePathError(f"':' not allowed in path: {raw!r}")
        if part.rstrip(". ").lower() == ".git":
            raise UnsafePathError(f"git internals not allowed: {raw!r}")
    return "/".join(parts)


def normalize_paths(paths: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    out = []
    for raw in paths:
        norm = safe_relpath(raw)
        if norm.lower() in seen:  # case-insensitive: Windows/macOS filesystems would collide
            raise UnsafePathError(f"duplicate path after normalization: {raw!r}")
        seen.add(norm.lower())
        out.append(norm)
    return out


@dataclass
class CommitResult:
    commit_hash: str
    created: bool  # False when the content was identical to the branch head, so nothing was committed


class GitWorkspace:
    def __init__(self, repo_dir: Path | str):
        self.repo = Path(repo_dir).resolve()
        self.worktrees = self.repo.parent / f"{self.repo.name}-worktrees"

    # ---- git plumbing ----

    def _git(self, *args: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                timeout=GIT_TIMEOUT, encoding="utf-8",
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorkspaceError(f"git {' '.join(args)} failed to run: {e}") from e
        if check and proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc

    def ensure_repo(self) -> None:
        if (self.repo / ".git").exists():
            return
        self.repo.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", "main")
        self._git(*_IDENTITY, "commit", "--allow-empty", "-m", "Initial commit")

    def branch_exists(self, branch: str) -> bool:
        return self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    def read_file(self, branch: str, path: str) -> Optional[str]:
        proc = self._git("show", f"{branch}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def list_files(self, branch: str) -> list[str]:
        out = self._git("ls-tree", "-r", "--name-only", branch).stdout
        return [line for line in out.splitlines() if line]

    def read_verified(self, commit: str, recorded: Mapping[str, str]) -> dict[str, str]:
        """
        Files at `commit`, with line endings normalized to LF. Raises ValueError unless they are exactly
        the files and contents the store recorded, so downstream stages work on what was really committed.
        """
        def lf(text: str) -> str:
            return text.replace("\r\n", "\n")

        try:
            committed = set(self.list_files(commit))
        except WorkspaceError as e:
            raise ValueError(f"cannot read commit {commit[:8]}: {e}") from e
        if committed != set(recorded):
            raise ValueError("committed files do not match the recorded artifact")
        files = {}
        for path in sorted(committed):
            content = self.read_file(commit, path)
            if content is None or lf(content) != lf(recorded[path]):
                raise ValueError(f"{path}: committed content does not match the recorded artifact")
            files[path] = lf(content)
        return files

    # ---- the one write operation ----

    def commit_files(
        self, branch: str, files: Mapping[str, str], remove: Iterable[str] = (), message: str = "Update",
    ) -> CommitResult:
        """
        Write `files` (and delete `remove`) on `branch`, creating it from the main checkout's HEAD if needed.
        Paths are validated before anything touches disk.
        """
        norm = normalize_paths(list(files) + list(remove))  # pure validation first: nothing touches disk yet
        write_paths, remove_paths = norm[: len(files)], norm[len(files):]
        if set(map(str.lower, write_paths)) & set(map(str.lower, remove_paths)):
            raise UnsafePathError("a path is both written and removed")
        contents = dict(zip(write_paths, files.values()))

        self.ensure_repo()
        self._git("check-ref-format", "--branch", branch)
        wt = self.worktrees / branch.replace("/", "__")
        self._remove_worktree(wt)  # clear leftovers from a crashed run
        wt.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(branch):
            self._git("worktree", "add", str(wt), branch)
        else:
            self._git("worktree", "add", "-b", branch, str(wt), "HEAD")
        try:
            root = wt.resolve()
            for rel, content in contents.items():
                target = (wt / rel).resolve()
                if root not in target.parents:  # belt and braces on top of safe_relpath (symlinks etc.)
                    raise UnsafePathError(f"path escapes workspace: {rel!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            for rel in remove_paths:
                target = (wt / rel).resolve()
                if root in target.parents and target.is_file():
                    target.unlink()

            self._git("add", "-A", cwd=wt)
            if not self._git("status", "--porcelain", cwd=wt).stdout.strip():
                return CommitResult(self._git("rev-parse", "HEAD", cwd=wt).stdout.strip(), created=False)
            self._git(*_IDENTITY, "commit", "-m", message, cwd=wt)
            return CommitResult(self._git("rev-parse", "HEAD", cwd=wt).stdout.strip(), created=True)
        finally:
            self._remove_worktree(wt)

    def _remove_worktree(self, wt: Path) -> None:
        if wt.exists():
            self._git("worktree", "remove", "--force", str(wt), check=False)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        self._git("worktree", "prune", check=False)
