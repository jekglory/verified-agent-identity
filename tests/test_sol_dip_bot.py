import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sol_dip_bot as bot  # noqa: E402


def run_bot(tmp_path, *args):
    env = {**os.environ, "ONCHAINOS_BIN": "/nonexistent/onchainos", "STATE_PATH": str(tmp_path / "state.json")}
    return subprocess.run([sys.executable, "sol_dip_bot.py", *args], capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_dry_run_once_offline(tmp_path):
    proc = run_bot(tmp_path, "--dry-run", "--once")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Running in dry-run mode" in proc.stdout
    assert "SOL dip bot started (DRY-RUN)" in proc.stdout
    assert (tmp_path / "state.json.paper").exists()


def test_live_mode_requires_onchainos(tmp_path):
    proc = run_bot(tmp_path, "--once")
    assert proc.returncode == 1
    assert "onchainos not found" in proc.stdout


def test_load_strategy_matches_json():
    s = bot.load_strategy()
    assert (s.timeframe, s.fast_period, s.slow_period) == ("5m", 5, 10)
    assert (s.stop_loss_pct, s.take_profit_pct, s.max_hold_bars) == (1.8, 5.0, 144)
    assert (s.size_pct, s.cooldown_bars, s.max_daily_trades, s.max_consecutive_losses) == (10.0, 2, 1, 2)


def test_ema_and_cross():
    assert bot.ema([1, 2, 3], 5) == [None, None, None]
    assert bot.ema([2, 4, 6, 8], 2) == [None, 3.0, 5.0, 7.0]
    assert bot.crossed_above([1.0, 3.0], [2.0, 2.0])
    assert not bot.crossed_above([3.0, 4.0], [2.0, 2.0])
    assert not bot.crossed_above([None, 3.0], [2.0, 2.0])


def test_exit_reasons():
    s = bot.Strategy()
    now = time.time()
    pos = bot.Position(entry_price=100.0, size_sol=1.0, cost_usdc=100.0, entry_ts=int(now))
    assert bot.exit_reason(pos, 98.2, now, s) == "STOP_LOSS"
    assert bot.exit_reason(pos, 105.0, now, s) == "TAKE_PROFIT"
    assert bot.exit_reason(pos, 101.0, now, s) is None
    assert bot.exit_reason(pos, 101.0, now + 144 * 300, s) == "MAX_HOLD_TIME"


def test_entry_blocks():
    s = bot.Strategy()
    now = time.time()
    assert bot.entry_block_reason(bot.State(), s, 0, now) is None
    assert "session trade limit" in bot.entry_block_reason(bot.State(), s, bot.MAX_TRADES_PER_SESSION, now)
    assert "daily" in bot.entry_block_reason(bot.State(trades_today=1), s, 0, now)
    assert "consecutive losses" in bot.entry_block_reason(bot.State(consecutive_losses=2), s, 0, now)
    assert "cooldown" in bot.entry_block_reason(bot.State(last_exit_ts=int(now)), s, 0, now)


def test_paper_round_trip(tmp_path):
    state = bot.State(paper_usdc=100.0, paper_sol=bot.GAS_RESERVE_SOL)
    api = bot.OnchainOS("/nonexistent/onchainos", paper=True, state=state)
    s = bot.Strategy()
    assert bot.open_position(api, state, s, "PAPER", 100.0)
    assert state.position and abs(state.position.size_sol - 0.1) < 1e-9
    assert abs(state.paper_usdc - 90.0) < 1e-9
    bot.close_position(api, state, "PAPER", 95.0, "STOP_LOSS")
    assert state.position is None and state.consecutive_losses == 1
    assert abs(state.history[-1]["pnl_usdc"] + 0.5) < 1e-9
    bot.save_state(state, str(tmp_path / "s.json"))
    assert bot.load_state(str(tmp_path / "s.json")).history[-1]["reason"] == "STOP_LOSS"


def test_adopt_sol_capital_then_exit():
    reserve = bot.GAS_RESERVE_SOL + bot.USDC_ACCOUNT_RENT_SOL
    state = bot.State(paper_usdc=0.0, paper_sol=0.0069)
    api = bot.OnchainOS("/nonexistent/onchainos", paper=True, state=state)
    assert bot.adopt_sol_position(api, state, 100.0)
    assert state.position and abs(state.position.size_sol - (0.0069 - reserve)) < 1e-12
    assert state.position.entry_price == 100.0
    bot.close_position(api, state, "PAPER", 105.0, "TAKE_PROFIT")
    assert state.position is None
    assert abs(state.paper_sol - reserve) < 1e-12
    assert abs(state.history[-1]["pnl_usdc"] - (0.0069 - reserve) * 5) < 1e-9


def test_adopt_skips_dust():
    state = bot.State(paper_usdc=0.0, paper_sol=0.003)
    api = bot.OnchainOS("/nonexistent/onchainos", paper=True, state=state)
    assert not bot.adopt_sol_position(api, state, 100.0)
    assert state.position is None
