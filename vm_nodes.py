"""Agent-sidecar discovery for cua VM nodes — stdlib only.

Every VM node can run its own copy of the agentd sidecar inside the guest
("agents inside VMs"). New VMs publish the sidecar port at creation
(`--service agent=8472`), and `cua sb list --json` then carries
`endpoints.agent`. VMs created before that get a managed fallback: a
persistent `cua sb port-forward <name> 8472:<loopback-port>` host process,
allocated from a fixed port range and tracked here for the server's lifetime.

All functions are best-effort like the other clients: a missing cua CLI, a
dead sandbox, or a spawn failure yields None/False/[], never an exception.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request

import cua_client

PROBE_TIMEOUT = 2          # seconds for a single /health or /conversations call
HEALTH_CACHE_TTL = 5.0     # seconds a node-health answer is reused
FALLBACK_PORT_BASE = 18500  # managed port-forward range: 18500..18549
FALLBACK_PORT_COUNT = 50
GUEST_PORT = 8472           # agentd sidecar port inside the guest

_pf_lock = threading.Lock()
_pf_procs: dict[str, tuple[subprocess.Popen, int]] = {}
"""Sandbox name -> (host port-forward process, host port). Lives as long as
the server process; the forward itself is a persistent host process."""
_pf_failed_at: dict[str, float] = {}
"""Sandbox name -> last failed spawn/forward attempt; respawns are backed off
so a dead forward is not retried on every poll."""
PF_RETRY_SECONDS = 60.0

_health_lock = threading.Lock()
_health_cache: dict[str, tuple[float, dict | None]] = {}
"""Sandbox name -> (monotonic timestamp, last /health dict)."""


def reset_cache() -> None:
    """Drop cached node health (tests call this)."""
    with _health_lock:
        _health_cache.clear()


def _sandbox(name: str) -> dict | None:
    if not cua_client.find_cua():
        return None
    try:
        for sb in cua_client.list_sandboxes():
            if isinstance(sb, dict) and sb.get("name") == name:
                return sb
    except Exception:  # noqa: BLE001 - listing drift is tolerated
        pass
    return None


def _published_endpoint(entry: dict | None) -> str | None:
    """endpoints.agent from a list entry, when the sandbox publishes it."""
    if not entry:
        return None
    endpoints = entry.get("endpoints")
    if isinstance(endpoints, dict):
        url = endpoints.get("agent")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def _port_free(port: int) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _ensure_port_forward(name: str) -> str | None:
    """Start (once) a managed `cua sb port-forward` for a legacy sandbox that
    does not publish its sidecar port. Returns http://127.0.0.1:<port>, or
    None when no free port in the range or the spawn failed. Retries are
    backed off so a wedged forward can't churn processes on every poll."""
    exe = cua_client.find_cua()
    if not exe:
        return None
    with _pf_lock:
        live = _pf_procs.get(name)
        if live and live[0].poll() is None:
            return f"http://127.0.0.1:{live[1]}"
        failed_at = _pf_failed_at.get(name, 0.0)
        if time.monotonic() - failed_at < PF_RETRY_SECONDS:
            return None
        port = next((FALLBACK_PORT_BASE + i for i in range(FALLBACK_PORT_COUNT)
                     if _port_free(FALLBACK_PORT_BASE + i)), None)
        if port is None:
            _pf_failed_at[name] = time.monotonic()
            return None
        try:
            proc = subprocess.Popen(
                [exe, "sb", "port-forward", name, f"{GUEST_PORT}:{port}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001 - spawn failure tolerated
            _pf_failed_at[name] = time.monotonic()
            return None
        _pf_procs[name] = (proc, port)
    print(f"vm_nodes: port-forward for {name} on 127.0.0.1:{port} "
          f"(pid {proc.pid})")
    return f"http://127.0.0.1:{port}"


def reap_dead_forwards(sandboxes: list[dict]) -> None:
    """Kill managed port-forwards whose sandbox no longer exists."""
    alive = {str(sb.get("name") or "") for sb in sandboxes
             if isinstance(sb, dict)}
    with _pf_lock:
        for name, (proc, _port) in list(_pf_procs.items()):
            if name not in alive and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                del _pf_procs[name]


def node_endpoint(name: str) -> str | None:
    """URL of the sandbox's in-guest agentd sidecar: the published endpoint
    when present, else a managed port-forward. None when unreachable."""
    published = _published_endpoint(_sandbox(name))
    if published:
        return published
    return _ensure_port_forward(name)


def node_health(name: str) -> dict | None:
    """GET <endpoint>/health -> dict, cached ~5s; None when no endpoint or
    the sidecar doesn't answer (never raises)."""
    with _health_lock:
        hit = _health_cache.get(name)
        if hit and time.monotonic() - hit[0] < HEALTH_CACHE_TTL:
            return hit[1]
    endpoint = node_endpoint(name)
    health = None
    if endpoint:
        try:
            with urllib.request.urlopen(endpoint + "/health",
                                        timeout=PROBE_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            health = data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            health = None
    with _health_lock:
        _health_cache[name] = (time.monotonic(), health)
    return health


def node_healthy(name: str) -> bool:
    """True when the node's sidecar answers /health with ok:true."""
    health = node_health(name)
    return bool(health and health.get("ok"))


def node_conversations(name: str) -> list | None:
    """GET <endpoint>/conversations -> list, or None when the node has no
    reachable sidecar (never raises)."""
    endpoint = node_endpoint(name)
    if not endpoint:
        return None
    try:
        with urllib.request.urlopen(endpoint + "/conversations",
                                    timeout=PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        convs = data.get("conversations") if isinstance(data, dict) else None
        return convs if isinstance(convs, list) else None
    except Exception:  # noqa: BLE001
        return None


def list_nodes() -> list[dict]:
    """One entry per sandbox: {name, online, endpoint}. `online` is the
    in-guest sidecar answering /health — NOT the sandbox's run state (a
    suspended VM is still listed, just offline). Empty when cua is absent."""
    if not cua_client.find_cua():
        return []
    try:
        sandboxes = cua_client.list_sandboxes()
    except Exception:  # noqa: BLE001
        return []
    reap_dead_forwards(sandboxes)
    out = []
    for sb in sandboxes:
        try:
            name = str(sb.get("name") or "")
        except AttributeError:
            continue
        if not name:
            continue
        out.append({"name": name,
                    "online": node_healthy(name),
                    "endpoint": _published_endpoint(sb)})
    return out
