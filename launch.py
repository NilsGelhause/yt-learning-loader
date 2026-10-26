"""Launch flow for the desktop icon: run tests -> show overview -> start the
server -> open the browser. The webpage only opens when every test passes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SETTINGS = ROOT / "data" / "settings.json"


def read_port() -> int:
    try:
        return int(json.loads(SETTINGS.read_text(encoding="utf-8")).get("port", 8471))
    except Exception:
        return 8471


def port_alive(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2)
        return True
    except Exception:
        return False


def wait_for_server(port: int, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_alive(port):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    print("Running test suite...\n")
    tests = subprocess.run([str(PY), str(ROOT / "tests" / "run.py")], cwd=str(ROOT))
    if tests.returncode != 0:
        print("\nTESTS FAILED — the webpage will NOT be opened.")
        print("Fix the failures above and start again.")
        input("\nPress Enter to close this window...")
        return 1

    print("\nAll tests passed — starting the server...\n")
    port = read_port()

    if port_alive(port):
        print(f"A server is already running on port {port} — opening the page.")
        webbrowser.open(f"http://127.0.0.1:{port}")
        return 0

    server = subprocess.Popen([str(PY), str(ROOT / "server.py")], cwd=str(ROOT))
    if wait_for_server(port):
        webbrowser.open(f"http://127.0.0.1:{port}")
        print("Close this window (or press Ctrl+C) to stop the server.")
        try:
            server.wait()
        except KeyboardInterrupt:
            server.terminate()
        return 0

    print("The server failed to start — see the output above.")
    input("\nPress Enter to close this window...")
    if server.poll() is None:
        server.terminate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
