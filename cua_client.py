"""cua (https://github.com/trycua/cua) CLI client — stdlib only.

The app drives cua sandboxes through the `cua` CLI (on Windows the scriptable
path; sandboxes are gVisor containers that need WSL2). This module locates the
CLI, reports its version, and lists sandboxes, tolerating CLI drift: unknown
output shapes yield an empty list, never an exception. Long-running create/
remove are not run here — server.py streams them as agent-style jobs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
"""Sandbox names double as CLI arguments — keep them tame."""

_CACHE_SECONDS = 5.0
_which_cache: tuple[float, str | None] | None = None

# Verified against the cua CLI reference (cua.ai/docs): `cua sandbox` has
# `ls`/`info`/`rm`/`exec`/`screenshot`; `sb` is the alias and `cua sb list`
# is documented. Try `--json` first (global flag), fall back to table text.
# Only three attempts: each is a subprocess; a slow daemon must not make the
# webapp wait a minute per page load.
_LIST_ARGV = (
    ("sb", "list", "--json"),
    ("sb", "ls"),
)
_LIST_TIMEOUT = 8
_LIST_CACHE_TTL = 5.0

_list_lock = threading.Lock()
_list_cache: tuple[float, list] | None = None
"""(monotonic timestamp, last result) — successes AND failures are cached so
a slow/dead cua daemon is never hammered by the polling tabs."""


def reset_cache() -> None:
    """Drop cached find_cua()/list_sandboxes()/version() results (tests call this)."""
    global _which_cache, _list_cache, _version_cache
    _which_cache = None
    _version_cache = None
    with _list_lock:
        _list_cache = None


def find_cua() -> str | None:
    """Absolute path to the cua CLI, or None if not installed. Cached for a
    few seconds so 3-4s polling does not rescan PATH constantly."""
    global _which_cache
    if _which_cache and time.monotonic() - _which_cache[0] < _CACHE_SECONDS:
        return _which_cache[1]
    try:
        found = shutil.which("cua")
    except Exception:
        found = None
    _which_cache = (time.monotonic(), found)
    return found


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name or ""))


def full_name(raw: str) -> str:
    """User input -> sandbox name: lowercased, prefixed ytl- (our mark so
    the machines this app made are recognizable in `cua sb list`)."""
    name = (raw or "").strip().lower()
    if name and not name.startswith("ytl-"):
        name = "ytl-" + name
    return name


def create_argv(name: str) -> list[str]:
    # --service agent=8472: publish the in-guest agentd sidecar port from
    # birth so the VM is an agent node without extra provisioning (vm_nodes).
    return ["sb", "create", "ubuntu", "--name", name, "--service", "agent=8472"]


def remove_argv(name: str) -> list[str]:
    # --force: `cua sb rm` asks for confirmation on a TTY and refuses to
    # delete from a background process without it.
    return ["sb", "rm", name, "--force"]


def resume_argv(name: str) -> list[str]:
    # cua has no `sb start` — a suspended/stopped sandbox wakes with `resume`.
    return ["sb", "resume", name]


_version_lock = threading.Lock()
_version_cache: tuple[float, str | None] | None = None
_VERSION_TTL = 300.0


def version(exe: str | None = None) -> str | None:
    """`cua --version` output, loosely parsed; None on any failure.
    Cached for a few minutes — the UI polls status every few seconds and the
    version cannot change underneath a running server."""
    global _version_cache
    with _version_lock:
        if _version_cache and time.monotonic() - _version_cache[0] < _VERSION_TTL:
            return _version_cache[1]
    exe = exe or find_cua()
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        result = None
        with _version_lock:
            _version_cache = (time.monotonic(), result)
        return None
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if not out:
        result = None
    else:
        # shapes seen across CLIs: "cua 1.2.3", "cua-cli 1.2.3", "1.2.3"
        m = re.search(r"(\d+(?:\.\d+)+(?:[-+][\w.]+)?)", out)
        result = m.group(1) if m else out.splitlines()[0][:40]
    with _version_lock:
        _version_cache = (time.monotonic(), result)
    return result


def _run_list(exe: str, args: tuple[str, ...]) -> str | None:
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_LIST_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _norm(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name") or entry.get("id") or entry.get("sandbox")
    if not name:
        return None
    status = str(entry.get("status") or entry.get("state") or "unknown").lower()
    if status in ("active", "started", "up", "ready"):
        status = "running"
    return {"name": str(name), "status": status,
            "image": str(entry.get("image") or entry.get("img") or ""),
            "kind": str(entry.get("kind") or ""),
            "endpoints": entry.get("endpoints") if isinstance(entry.get("endpoints"), dict) else {}}


def _parse_json(text: str) -> list[dict] | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        for key in ("sandboxes", "items", "list", "data", "rows"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    if not isinstance(data, list):
        return None
    out = [_norm(e) for e in data]
    return [e for e in out if e]


def _parse_table(text: str) -> list[dict]:
    """Best-effort parse of a whitespace-aligned table (the pre-`--json`
    shape): find the header, map columns named NAME/STATUS(/IMAGE|STATE),
    then split data rows on 2+ spaces."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header_idx = next((i for i, ln in enumerate(lines)
                       if re.search(r"\bNAME\b", ln, re.IGNORECASE)), None)
    if header_idx is None:
        return []
    cols = [c.strip().upper() for c in re.split(r"\s{2,}", lines[header_idx].strip())]

    def col(*names: str) -> int | None:
        for n in names:
            if n in cols:
                return cols.index(n)
        return None

    name_i = col("NAME", "SANDBOX", "ID")
    status_i = col("STATUS", "STATE")
    image_i = col("IMAGE", "IMG")
    if name_i is None:
        return []
    rows = []
    for ln in lines[header_idx + 1:]:
        cells = re.split(r"\s{2,}", ln.strip())
        if len(cells) <= name_i:
            continue
        status = cells[status_i].lower() if status_i is not None and status_i < len(cells) else "unknown"
        if status in ("active", "started", "up", "ready"):
            status = "running"
        rows.append({
            "name": cells[name_i].strip(),
            "status": status,
            "image": cells[image_i].strip() if image_i is not None and image_i < len(cells) else "",
            "kind": "",
            "endpoints": {},
        })
    return rows


def list_sandboxes(exe: str | None = None) -> list[dict]:
    """Sandboxes as [{"name", "status", "image", "kind", "endpoints"}]. Tries
    `sb list --json` then the table form of `sb ls`; any failure or unparseable
    output -> []. Results (including failures) are cached for a few seconds,
    and the fetch itself is serialized — the UI polls several endpoints every
    few seconds and concurrent callers must not each spawn a cua subprocess
    (a cold cua daemon makes every attempt eat its timeout)."""
    global _list_cache
    with _list_lock:
        if _list_cache and time.monotonic() - _list_cache[0] < _LIST_CACHE_TTL:
            return _list_cache[1]
        exe = exe or find_cua()
        result: list[dict] = []
        if exe:
            for args in _LIST_ARGV:
                text = _run_list(exe, args)
                if text is None:
                    continue  # command failed — maybe this build lacks it; try the next
                parsed = _parse_json(text) if "--json" in args else None
                if parsed is None:
                    parsed = _parse_table(text)
                result = parsed  # the list command answered; trust its shape
                break
        _list_cache = (time.monotonic(), result)
        return result
