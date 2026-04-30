"""In-app self-updater backed by the local `git` CLI.

The repo is the source of truth: `status` describes the working tree,
`fetch` pulls remote refs, `apply` does a fast-forward `git pull`, and
`schedule_restart` re-execs the running process so a freshly-pulled
gjallarhorn.py starts serving.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


class GitError(RuntimeError):
    pass


async def _git(*args: str, check: bool = True) -> tuple[int, str, str]:
    """Run `git` in the repo root. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {err or out}")
    return proc.returncode or 0, out, err


async def _commit_info(ref: str) -> dict | None:
    """Return {sha, short, subject, author, date} for a ref, or None if missing."""
    code, out, _ = await _git(
        "log", "-1", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%cI", ref, check=False,
    )
    if code != 0 or not out:
        return None
    parts = out.split("\x1f")
    if len(parts) != 5:
        return None
    sha, short, subject, author, date = parts
    return {"sha": sha, "short": short, "subject": subject, "author": author, "date": date}


async def get_status(*, do_fetch: bool = False) -> dict[str, Any]:
    """Inspect the working copy and (optionally) refresh remotes first.

    Returns a JSON-shaped dict the UI can render directly.
    """
    if not (REPO_ROOT / ".git").exists():
        return {"ok": False, "error": "Not a git checkout — updates unavailable."}

    fetch_error: str | None = None
    if do_fetch:
        code, _, err = await _git("fetch", "--prune", "origin", check=False)
        if code != 0:
            fetch_error = err or "git fetch failed"

    # Branch + upstream
    _, branch, _ = await _git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    code, upstream, _ = await _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False,
    )
    has_upstream = code == 0 and bool(upstream)

    # Remote URL (informational)
    _, remote_url, _ = await _git("remote", "get-url", "origin", check=False)

    # Dirty?
    code, porcelain, _ = await _git("status", "--porcelain", check=False)
    dirty_files = [ln for ln in porcelain.splitlines() if ln.strip()]
    dirty = bool(dirty_files)

    # Current and target commits
    current = await _commit_info("HEAD")
    target = await _commit_info(upstream) if has_upstream else None

    ahead = behind = 0
    if has_upstream:
        code, lr, _ = await _git(
            "rev-list", "--left-right", "--count", f"{upstream}...HEAD", check=False,
        )
        if code == 0 and lr:
            try:
                b_str, a_str = lr.split()
                behind, ahead = int(b_str), int(a_str)
            except ValueError:
                pass

    # Will pip deps change after a pull?
    requirements_changed = False
    if has_upstream and behind > 0:
        code, diff_names, _ = await _git(
            "diff", "--name-only", "HEAD", upstream, check=False,
        )
        if code == 0:
            requirements_changed = "requirements.txt" in diff_names.splitlines()

    return {
        "ok": True,
        "branch": branch or None,
        "upstream": upstream if has_upstream else None,
        "remote_url": remote_url or None,
        "current": current,
        "target": target,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "dirty_files": dirty_files[:20],  # cap so we don't dump huge lists
        "requirements_changed": requirements_changed,
        "fetch_error": fetch_error,
    }


async def apply_update() -> dict[str, Any]:
    """Fast-forward pull. Refuses on dirty trees or non-FF histories."""
    status = await get_status(do_fetch=True)
    if not status.get("ok"):
        raise GitError(status.get("error", "update unavailable"))
    if status["dirty"]:
        raise GitError("Working tree has local changes; refusing to update.")
    if not status.get("upstream"):
        raise GitError("No upstream branch configured.")
    if status["behind"] == 0:
        return {"ok": True, "updated": False, "status": status}

    code, out, err = await _git("pull", "--ff-only", check=False)
    if code != 0:
        raise GitError(f"git pull failed: {err or out}")

    new_status = await get_status(do_fetch=False)
    return {
        "ok": True,
        "updated": True,
        "status": new_status,
        "requirements_changed": status["requirements_changed"],
        "pull_output": out,
    }


def schedule_restart(delay_s: float = 0.5) -> None:
    """Re-exec the current Python process after a short delay so the HTTP
    response can flush. Works on Windows and POSIX — `os.execv` spawns a new
    process on Windows, replaces it on POSIX. The listening socket cycles
    briefly during the handover."""
    loop = asyncio.get_event_loop()

    def _do_restart() -> None:
        log.warning("restarting process: %s %s", sys.executable, sys.argv)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            # If exec fails, exit hard — a process supervisor will bring us back.
            log.error("execv failed (%s); exiting so supervisor restarts us", e)
            os._exit(1)

    loop.call_later(delay_s, _do_restart)
