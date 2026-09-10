"""cc_session — shared helpers for inspecting a live Claude Code session.

Given a tmux session name, find the `claude` process, locate its active transcript
jsonl, and read the busy/idle state and context-window size from the last turn.

The chain (robust to restarts):
  tmux session -> claude PID -> /proc/<pid>/cwd -> CC project dir
  (cwd with every '/' and '.' replaced by '-') -> the transcript jsonl in it.

This module is intentionally free of any deployment-specific detail; everything is
derived from the running process and $HOME.
"""
from __future__ import annotations
import json, os, re, subprocess
from pathlib import Path

PROJECTS = Path.home() / ".claude/projects"
# Context window of the model the agents run on. The default targets a large
# (~1M-token) Claude model; context-monitor's rotate_threshold leaves headroom under
# this for the agent to author its wrap. Override via CC_CONTEXT_WINDOW for a
# smaller-context model (and lower each agent's rotate_threshold to match).
WINDOW = int(os.environ.get("CC_CONTEXT_WINDOW", 1_000_000))


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def session_exists(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def find_claude_pid(session: str) -> int | None:
    """Find the `claude` process inside a tmux session by walking pane descendants."""
    pane_pids = sh("tmux", "list-panes", "-t", session, "-F", "#{pane_pid}").split()
    if not pane_pids:
        return None
    ps = sh("ps", "-e", "-o", "pid=,ppid=,comm=")
    children: dict[int, list[int]] = {}
    comm: dict[int, str] = {}
    for ln in ps.splitlines():
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, c = int(parts[0]), int(parts[1]), parts[2]
        comm[pid] = c
        children.setdefault(ppid, []).append(pid)
    for start in pane_pids:
        stack = [int(start)]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if comm.get(pid, "").startswith("claude"):
                return pid
            stack.extend(children.get(pid, []))
    return None


def project_dir_for(pid: int) -> Path | None:
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    d = PROJECTS / re.sub(r"[/.]", "-", cwd)
    return d if d.exists() else None


def freshest_jsonl(d: Path, min_size: int = 50_000) -> Path | None:
    cands = [(p.stat().st_mtime, p) for p in d.glob("*.jsonl")
             if p.stat().st_size > min_size]
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def jsonl_for_pid(pid: int) -> Path | None:
    """Find the EXACT transcript a claude PID has open via /proc/<pid>/fd.

    Critical when multiple agent sessions share one CC project dir (same cwd):
    freshest-in-dir is ambiguous, but the open fd is unique to this process.
    """
    fddir = f"/proc/{pid}/fd"
    try:
        for fd in os.listdir(fddir):
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if target.endswith(".jsonl") and "/.claude/projects/" in target:
                p = Path(target)
                if p.exists():
                    return p
    except OSError:
        return None
    return None


def last_assistant_usage(jsonl_path: Path) -> dict | None:
    """Scan the jsonl tail for the most recent assistant message's usage block."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        usage = (d.get("message") or {}).get("usage")
        if usage:
            return usage
    return None


def context_used(usage: dict) -> int:
    return (int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0)))


def session_context(session: str) -> tuple[int, Path] | None:
    """Return (context_tokens, jsonl_path) for a live tmux session, or None if the
    session/process/transcript/usage can't be resolved."""
    if not session_exists(session):
        return None
    pid = find_claude_pid(session)
    if not pid:
        return None
    jsonl = jsonl_for_pid(pid)
    if jsonl is None:
        d = project_dir_for(pid)
        jsonl = freshest_jsonl(d) if d else None
    if not jsonl:
        return None
    usage = last_assistant_usage(jsonl)
    if not usage:
        return None
    return context_used(usage), jsonl


def project_dir_for_home() -> Path:
    """CC project dir for an agent whose cwd is $HOME (the default spawn cwd)."""
    return PROJECTS / re.sub(r"[/.]", "-", str(Path.home()))


def context_from_jsonl(jsonl_path: Path) -> tuple[int, Path] | None:
    """Read context size from an explicit transcript path (deterministic — used
    when the agent's session_id is known from the registry)."""
    if not jsonl_path.exists():
        return None
    usage = last_assistant_usage(jsonl_path)
    return (context_used(usage), jsonl_path) if usage else None


def tmux_inject(session: str, text: str) -> None:
    """Type `text` into a tmux session and submit it (literal, then Enter).

    Both send-keys calls are timeout-bounded so a wedged tmux client can never block
    the caller (a hung send-keys would otherwise freeze the whole dispatcher loop and
    starve every agent). On timeout we reap any stray hung send-keys for this session
    and re-raise, so the caller skips this inject and retries next cycle.
    """
    for keys in (["-l", text], ["Enter"]):
        try:
            subprocess.run(["tmux", "send-keys", "-t", session, *keys],
                           capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            # Anchor the match ( |$) so e.g. session 'agent' can't reap 'agent-2'.
            subprocess.run(["pkill", "-f", f"send-keys -t {session}( |$)"],
                           capture_output=True, timeout=5)
            raise
