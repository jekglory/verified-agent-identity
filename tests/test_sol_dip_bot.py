import subprocess
import sys


def test_sol_dip_bot_dry_run_once():
    """Run the bot in dry-run single-cycle mode and expect exit code 0."""
    proc = subprocess.run([
        sys.executable,
        "sol_dip_bot.py",
        "--dry-run",
        "--once",
    ], capture_output=True, text=True)
    # Should exit successfully
    assert proc.returncode == 0, f"Process failed: {proc.stderr}"
    # Basic smoke checks on output
    out = proc.stdout
    assert "Running in dry-run mode" in out
    assert "SOL dip bot — LIVE TRADING MODE" in out
