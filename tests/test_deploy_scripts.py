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
        success_pos = text.index("record_sync_status succeeded 0")
        doctor_pos = min(text.index("--doctor-strict"), text.index("--doctor"))
        self.assertLess(maintenance_pos, doctor_pos)
        self.assertLess(audit_prune_pos, doctor_pos)
        self.assertLess(email_prune_pos, doctor_pos)
        self.assertLess(success_pos, doctor_pos)

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

    def test_public_market_sync_keeps_research_report_on_adjusted_data(self):
        script = ROOT / "deploy" / "sync-market-public.sh"
        text = script.read_text(encoding="utf-8")

        self.assertIn('REPORT_ADJUST="${OWQ_REPORT_ADJUST:-hfq}"', text)
        self.assertIn('REPORT_MIN_CODES="${OWQ_REPORT_MIN_REPRESENTATIVE_CODES:-${OWQ_MARKET_MIN_REAL_CODES:-300}}"', text)
        self.assertIn('REPORT_SOURCE="${OWQ_REPORT_SOURCE:-akshare}"', text)
        self.assertIn('REPORT_MARKET_LIMIT="${OWQ_REPORT_MARKET_LIMIT:-$(( REPORT_MIN_CODES + 100 ))}"', text)
        self.assertIn('PREDICTIONS_CSV="${OWQ_PREDICTIONS_CSV:-reports/predictions.csv}"', text)
        self.assertIn('UNIVERSE_MODE="${OWQ_MARKET_UNIVERSE_MODE:-representative}"', text)
        self.assertIn('DATA_UNIVERSE_STATUS="${OWQ_MARKET_DATA_UNIVERSE_STATUS:-L}"', text)
        self.assertIn('REPORT_UNIVERSE_STATUS="${OWQ_REPORT_UNIVERSE_STATUS:-all}"', text)
        self.assertIn('OWQ_REPORT_ADJUST=none', text)
        self.assertIn('--source "$REPORT_SOURCE"', text)
        self.assertIn('--adjust "$REPORT_ADJUST"', text)
        self.assertIn('--status "$DATA_UNIVERSE_STATUS"', text)
        self.assertIn('--status "$REPORT_UNIVERSE_STATUS"', text)
        self.assertIn('--universe-mode "$UNIVERSE_MODE"', text)
        self.assertIn('--codes-csv "$PREDICTIONS_CSV"', text)
        self.assertIn('--min-representative-codes "$REPORT_MIN_CODES"', text)
        self.assertIn('--limit "$REPORT_MARKET_LIMIT"', text)
        self.assertIn('--predictions-csv "$PREDICTIONS_CSV"', text)
        self.assertIn('--market-include-codes-csv "$PREDICTIONS_CSV"', text)
        self.assertIn("--strict-representative-codes", text)
        self.assertIn('"$REPORT_ADJUST" != "$DATA_ADJUST" || "$REPORT_UNIVERSE_STATUS" != "$DATA_UNIVERSE_STATUS"', text)
        self.assertNotIn('--adjust "${OWQ_REPORT_ADJUST:-$APP_ADJUST}"', text)


if __name__ == "__main__":
    unittest.main()
