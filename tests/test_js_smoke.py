"""Frontend smoke coverage: every tab must render without runtime errors.

`node --check` only parses the JS; this test executes core.js + all tab
scripts against a stubbed DOM and renders each tab once, catching
reference errors (e.g. use-before-declaration) and typos in render paths.
Skipped when node is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SMOKE = Path(__file__).resolve().parent / "js_smoke.js"


@unittest.skipUnless(NODE, "node is not installed")
class JsSmokeTests(unittest.TestCase):

    def test_every_tab_renders_without_runtime_errors(self):
        """Each registered tab renders clean against the stub DOM."""
        proc = subprocess.run(
            [NODE, str(SMOKE)], capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"js_smoke failed:\n{proc.stdout}\n{proc.stderr}",
        )
