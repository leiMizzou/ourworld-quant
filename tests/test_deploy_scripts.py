from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# The public market-sync script is zsh; the syntax check is skipped where zsh is absent
# (e.g. Linux CI) and runs on the maintainer's macOS host.
HAS_ZSH = shutil.which("zsh") is not None


class DeployScriptsTest(unittest.TestCase):
    @unittest.skipUnless(HAS_ZSH, "requires zsh on PATH")
    def test_public_market_sync_script_is_executable_and_valid_zsh(self):
        script = ROOT / "deploy" / "sync-market-public.sh"

        self.assertTrue(script.exists())
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
        result = subprocess.run(["zsh", "-n", str(script)], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_market_sync_runs_sqlite_maintenance_before_doctor(self):
        script = ROOT / "deploy" / "sync-market-public.sh"
        text = script.read_text(encoding="utf-8")

        maintenance_pos = text.index("--sqlite-maintenance")
        audit_prune_pos = text.index("--prune-audit-log")
        email_prune_pos = text.index("--prune-email-login-sessions")
        doctor_pos = min(text.index("--doctor-strict"), text.index("--doctor"))
        self.assertLess(maintenance_pos, doctor_pos)
        self.assertLess(audit_prune_pos, doctor_pos)
        self.assertLess(email_prune_pos, doctor_pos)

    def test_public_market_sync_records_start_success_and_failure_status(self):
        script = ROOT / "deploy" / "sync-market-public.sh"
        text = script.read_text(encoding="utf-8")

        self.assertIn("record_sync_status started 0", text)
        self.assertIn("trap on_exit EXIT", text)
        self.assertIn("record_sync_status succeeded", text)
        self.assertIn("record_sync_status failed", text)
        self.assertIn("--record-market-sync-status", text)
        self.assertIn("--market-sync-exit-code", text)
        self.assertNotIn("local status=", text)

    def test_public_market_sync_keeps_non_strict_doctor_diagnostic_only(self):
        script = ROOT / "deploy" / "sync-market-public.sh"
        text = script.read_text(encoding="utf-8")

        self.assertIn('"$PYTHON" -m src.app.server --doctor-strict', text)
        self.assertIn('"$PYTHON" -m src.app.server --doctor || true', text)


if __name__ == "__main__":
    unittest.main()
