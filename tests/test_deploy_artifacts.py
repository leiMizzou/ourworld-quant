from __future__ import annotations

import plistlib
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# These artifacts are macOS deploy targets: the scripts are zsh and the launchd plists
# bake in this machine's absolute paths. They validate the maintainer's local setup, so
# they only run where that environment exists (skipped on Linux CI, run on the Mac).
HAS_BIN_ZSH = Path("/bin/zsh").exists()
IS_MACOS = sys.platform == "darwin"


class DeployArtifactsTest(unittest.TestCase):
    @unittest.skipUnless(HAS_BIN_ZSH, "requires /bin/zsh (macOS deploy scripts)")
    def test_deploy_shell_scripts_parse(self):
        for path in [
            ROOT / "deploy" / "check-public.sh",
            ROOT / "deploy" / "install-launchd.sh",
            ROOT / "deploy" / "run-public-app.sh",
            ROOT / "deploy" / "sync-market-public.sh",
            ROOT / "deploy" / "uninstall-launchd.sh",
        ]:
            with self.subTest(path=path.name):
                result = subprocess.run(["/bin/zsh", "-n", str(path)], cwd=ROOT, check=False, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(IS_MACOS, "launchd plists validate the maintainer's macOS deploy paths")
    def test_launchd_plists_are_valid_and_log_to_data_logs(self):
        for path in sorted((ROOT / "deploy" / "launchd").glob("*.plist")):
            with self.subTest(path=path.name):
                payload = plistlib.loads(path.read_bytes())
                self.assertIn("Label", payload)
                self.assertEqual(payload["EnvironmentVariables"]["OWQ_ENV_FILE"], str(ROOT / "deploy" / "public.env"))
                self.assertEqual(payload["WorkingDirectory"], "/tmp")
                self.assertEqual(payload["ProgramArguments"][0:2], ["/bin/zsh", "-lc"])
                self.assertIn(f"cd {ROOT}", payload["ProgramArguments"][2])
                self.assertTrue(payload["StandardOutPath"].startswith(str(ROOT / "data" / "logs")))
                self.assertTrue(payload["StandardErrorPath"].startswith(str(ROOT / "data" / "logs")))

    def test_launchd_installer_uses_private_runtime_copies(self):
        text = (ROOT / "deploy" / "install-launchd.sh").read_text(encoding="utf-8")
        for expected in [
            "Library/Application Support/OurWorldsQuant",
            "OWQ_ROOT_DIR",
            "OWQ_SECRET_FILE",
            "public.env",
            "app.secret",
            "PlistBuddy",
            "for attempt in 1 2 3",
            "launchctl bootstrap",
        ]:
            self.assertIn(expected, text)

    def test_launchd_runtime_secret_file_overrides_public_env(self):
        for path in [ROOT / "deploy" / "run-public-app.sh", ROOT / "deploy" / "sync-market-public.sh"]:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn('RUNTIME_SECRET_FILE="${OWQ_SECRET_FILE:-}"', text)
                self.assertIn('export OWQ_SECRET_FILE="$RUNTIME_SECRET_FILE"', text)

    def test_public_check_script_distinguishes_beta_from_formal_readiness(self):
        text = (ROOT / "deploy" / "check-public.sh").read_text(encoding="utf-8")
        for expected in [
            "OWQ_ALLOW_PUBLIC_BETA",
            "email_sending,email_dev_auth_public",
            "last exit code = 0",
            'payload.get("warnings", [])',
            "/livez",
            "/healthz",
            "/readyz",
            "/metrics",
            'payload.get("detail") == "summary"',
            "/data-status",
            "--verify-app-backup",
            "--restore-app-backup",
            "readyz body missing",
            'touch "$ready_body"',
            "latest app backup verifies",
            "latest app backup restore drill passed",
            "/robots.txt",
            "/sitemap.xml",
            "com.ourworlds.quant.app",
            "com.ourworlds.quant.market-sync",
            "check_public_sensitive_content",
            "public response bodies do not expose configured secret values or local paths",
            "check_public_register_page",
            "public register page does not expose email dev-auth verification links",
            "check_public_head_routes",
            "public HEAD /app redirects unauthenticated users to /login",
            "for protected_route in /account/consent",
            "for route in /register /forgot-password /login /showcase/public /forum /terms /privacy /risk /support",
            "HEAD ${PUBLIC_BASE_URL}/support missing noindex header",
            "robots disallows support request page",
            '"/support"',
            "local routes=(/ /register /forgot-password /login /support",
            "check_public_email_confirm_flow",
            "owq_email_confirm",
            "public email verification keeps token out of account setup HTML",
            "public email confirmation GET consumed token or created user",
        ]:
            self.assertIn(expected, text)

    def test_public_env_example_has_required_production_keys(self):
        text = (ROOT / "deploy" / "public.env.example").read_text(encoding="utf-8")
        for name in [
            "OWQ_ENV=production",
            "OWQ_PUBLIC_BASE_URL=https://quant.ourworlds.app",
            "OWQ_SECRET_FILE=data/app.secret",
            "OWQ_ADMIN_USER_IDS=",
            "OWQ_AUDIT_RETENTION_DAYS=400",
            "OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS=30",
            "OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS=72",
            "OWQ_SERVER_ERROR_WINDOW_HOURS=24",
            "OWQ_RATE_LIMITS_DISABLED=0",
            "OWQ_LEGAL_CONSENT_REQUIRED=1",
            "OWQ_EMAIL_DEV_AUTH=0",
            "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=0",
            "OWQ_MARKET_MIN_REAL_CODES=300",
            "OWQ_PREDICTIONS_MIN_CODES=10",
            "OWQ_MARKET_SYNC_MAX_AGE_HOURS=36",
            "OWQ_MARKET_UNIVERSE_MODE=representative",
            "OWQ_MARKET_DATA_UNIVERSE_STATUS=L",
            "OWQ_REPORT_UNIVERSE_STATUS=all",
            "OWQ_REPORT_SOURCE=akshare",
            "OWQ_SYNC_PRUNE_AUDIT=1",
            "OWQ_SYNC_PRUNE_EMAIL_LOGIN=1",
            "OWQ_REPORT_MIN_REPRESENTATIVE_CODES=300",
            "OWQ_REPORT_MARKET_LIMIT=400",
        ]:
            self.assertIn(name, text)

    def test_market_sync_script_prunes_expired_operational_records(self):
        text = (ROOT / "deploy" / "sync-market-public.sh").read_text(encoding="utf-8")
        self.assertIn('SYNC_PRUNE_AUDIT="${OWQ_SYNC_PRUNE_AUDIT:-1}"', text)
        self.assertIn('SYNC_PRUNE_EMAIL_LOGIN="${OWQ_SYNC_PRUNE_EMAIL_LOGIN:-1}"', text)
        self.assertIn("--prune-audit-log", text)
        self.assertIn("--prune-email-login-sessions", text)

    @unittest.skipUnless(HAS_BIN_ZSH, "requires /bin/zsh (macOS deploy scripts)")
    def test_public_env_example_can_be_sourced_by_zsh(self):
        script = """
set -euo pipefail
set -a
source deploy/public.env.example
set +a
[[ "$OWQ_EMAIL_FROM_NAME" == "OurWorlds Quant" ]]
[[ "$OWQ_PUBLIC_BASE_URL" == "https://quant.ourworlds.app" ]]
[[ "$OWQ_MARKET_SOURCE" == "tushare" ]]
"""
        result = subprocess.run(["/bin/zsh", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(HAS_BIN_ZSH, "requires /bin/zsh (macOS deploy scripts)")
    def test_dotenv_example_can_be_sourced_by_zsh(self):
        script = """
set -euo pipefail
set -a
source .env.example
set +a
[[ "$OWQ_EMAIL_FROM_NAME" == "OurWorlds Quant" ]]
[[ "$OWQ_SECRET_FILE" == "data/app.secret" ]]
"""
        result = subprocess.run(["/bin/zsh", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
