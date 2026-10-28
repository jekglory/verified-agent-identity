#!/usr/bin/env python3
"""SOL buy-dip bot (EMA cross) trading SOL/USDC on Solana via the OKX Onchain OS CLI.

Entry: buy SOL with USDC when the fast EMA crosses above the slow EMA on closed bars.
Exit:  sell the SOL back to USDC on stop loss, take profit, or max hold time.
All parameters come from sol_strategy.json; see README for env vars and flags.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from telegram_chat import TelegramChat, ask_llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.getenv("STRATEGY_PATH", os.path.join(BASE_DIR, "sol_strategy.json"))
STATE_PATH = os.getenv("STATE_PATH", os.path.join(BASE_DIR, "sol_dip_bot_state.json"))
LOG_PATH = os.path.join(BASE_DIR, "sol_dip_bot.log")

CHAIN = "solana"
NATIVE_SOL = "11111111111111111111111111111111"  # native SOL for swaps
WSOL_ADDRESS = "So11111111111111111111111111111111111111112"  # wSOL for market data
USDC_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC (Solana mainnet)

BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400}
KLINE_LIMIT = 100


def _default_onchainos_bin() -> str:
    return shutil.which("onchainos") or os.path.expanduser("~/.local/bin/onchainos")


ONCHAINOS_BIN = os.getenv("ONCHAINOS_BIN") or _default_onchainos_bin()
MAX_TRADES_PER_SESSION = int(os.getenv("MAX_TRADES_PER_SESSION", "10"))
MIN_ORDER_USDC = float(os.getenv("MIN_ORDER_USDC", "0.1"))
CAPITAL_ASSET = os.getenv("CAPITAL_ASSET", "SOL").upper()  # SOL: adopt wallet SOL as a position when flat
GAS_RESERVE_SOL = float(os.getenv("GAS_RESERVE_SOL", "0.001"))  # SOL never traded, kept for fees
USDC_ACCOUNT_RENT_SOL = 0.00204  # one-time rent to create the wallet's USDC token account
MAX_ORDER_USDC = float(os.getenv("MAX_ORDER_USDC", "0"))  # 0 = no cap
SLIPPAGE_PCT = os.getenv("SLIPPAGE_PCT", "1")  # empty string = CLI autoSlippage
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
PAPER_USDC = float(os.getenv("PAPER_USDC", "100"))
PAPER_SOL = float(os.getenv("PAPER_SOL", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

CLOSE_REQUESTED = threading.Event()  # set by Telegram /close, handled by the main loop
WAKE = threading.Event()  # interrupts the poll sleep
RUNTIME: dict[str, Any] = {}  # latest market snapshot for /status and AI chat

logger = logging.getLogger("sol_dip_bot")


# --------------------------------------------------------------------------- config


@dataclass
class Strategy:
    timeframe: str = "5m"
    fast_period: int = 5
    slow_period: int = 10
    stop_loss_pct: float = 1.8
    take_profit_pct: float = 5.0
    max_hold_bars: int = 144
    size_pct: float = 10.0
    cooldown_bars: int = 2
    max_daily_trades: int = 1
    max_consecutive_losses: int = 2

    @property
    def bar_seconds(self) -> int:
        return BAR_SECONDS[self.timeframe]


def load_strategy(path: str = STRATEGY_PATH) -> Strategy:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    s = Strategy(
        timeframe=cfg["instrument"]["timeframe"],
        fast_period=int(cfg["entry"]["fast_period"]),
        slow_period=int(cfg["entry"]["slow_period"]),
        stop_loss_pct=float(cfg["exit"]["stop_loss"]["pct"]),
        take_profit_pct=float(cfg["exit"]["take_profit"]["pct"]),
        size_pct=float(os.getenv("SIZE_PCT") or cfg["sizing"]["pct"]),
    )
    for item in cfg["exit"].get("other", []):
        if item.get("type") == "time_exit":
            s.max_hold_bars = int(item["max_bars"])
    for item in cfg.get("filters", []):
        if item.get("type") == "cooldown":
            s.cooldown_bars = int(item["bars"])
    for item in cfg.get("risk_overlays", []):
        if item.get("type") == "max_daily_trades":
            s.max_daily_trades = int(item["n"])
        elif item.get("type") == "session_loss_pause":
            s.max_consecutive_losses = int(item["max_consecutive_losses"])
    if s.timeframe not in BAR_SECONDS:
        raise ValueError(f"Unsupported timeframe {s.timeframe!r}")
    if not 0 < s.fast_period < s.slow_period:
        raise ValueError("fast_period must be > 0 and < slow_period")
    return s


# --------------------------------------------------------------------------- state


@dataclass
class Position:
    entry_price: float
    size_sol: float
    cost_usdc: float
    entry_ts: int
    tx_hash: str = ""


@dataclass
class State:
    position: Position | None = None
    day: str = ""
    trades_today: int = 0
    consecutive_losses: int = 0
    last_exit_ts: int = 0
    last_signal_bar_ts: int = 0
    paused: bool = False
    paper_usdc: float = PAPER_USDC
    paper_sol: float = PAPER_SOL
    history: list[dict[str, Any]] = field(default_factory=list)

    def roll_day(self, now: float) -> None:
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.trades_today = 0
            self.consecutive_losses = 0


def load_state(path: str = STATE_PATH) -> State:
    if not os.path.exists(path):
        return State()
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    pos = raw.pop("position", None)
    state = State(**raw)
    state.position = Position(**pos) if pos else None
    return state


def save_state(state: State, path: str = STATE_PATH) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(state), fh, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- utils


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    logger.info(msg)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as resp:
            resp.read()
    except Exception as exc:  # notifications must never crash the bot
        log(f"Telegram notify failed: {exc}")


def notify(msg: str) -> None:
    log(msg)
    send_telegram_message(msg)


def ema(series: list[float], period: int) -> list[float]:
    """EMA seeded with the SMA of the first `period` values; earlier entries are None."""
    if len(series) < period:
        return [None] * len(series)  # type: ignore[list-item]
    k = 2 / (period + 1)
    out: list[Any] = [None] * (period - 1)
    out.append(sum(series[:period]) / period)
    for value in series[period:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def crossed_above(fast: list[float], slow: list[float]) -> bool:
    if len(fast) < 2 or None in (fast[-2], fast[-1], slow[-2], slow[-1]):
        return False
    return fast[-2] <= slow[-2] and fast[-1] > slow[-1]


def exit_reason(pos: Position, price: float, now: float, s: Strategy) -> str | None:
    pnl_pct = round((price - pos.entry_price) / pos.entry_price * 100, 6)
    if pnl_pct <= -s.stop_loss_pct:
        return "STOP_LOSS"
    if pnl_pct >= s.take_profit_pct:
        return "TAKE_PROFIT"
    if now - pos.entry_ts >= s.max_hold_bars * s.bar_seconds:
        return "MAX_HOLD_TIME"
    return None


def entry_block_reason(state: State, s: Strategy, session_trades: int, now: float) -> str | None:
    if state.position:
        return "position already open"
    if state.paused:
        return "paused via Telegram /pause"
    if session_trades >= MAX_TRADES_PER_SESSION:
        return f"session trade limit reached ({session_trades}/{MAX_TRADES_PER_SESSION})"
    if state.trades_today >= s.max_daily_trades:
        return f"daily trade limit reached ({state.trades_today}/{s.max_daily_trades})"
    if state.consecutive_losses >= s.max_consecutive_losses:
        return f"paused after {state.consecutive_losses} consecutive losses (resets at UTC 00:00)"
    if now < state.last_exit_ts + s.cooldown_bars * s.bar_seconds:
        return "cooldown after last exit"
    return None


# --------------------------------------------------------------------------- onchainos


class OnchainOS:
    """Thin wrapper over the onchainos CLI. In paper mode swaps and balances are simulated."""

    def __init__(self, binary: str, paper: bool, state: State):
        self.binary = binary
        self.paper = paper
        self.state = state
        self.offline = paper and not (os.path.isfile(binary) and os.access(binary, os.X_OK))

    def run(self, *args: str) -> Any:
        proc = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=180)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"onchainos {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        if not payload.get("ok"):
            raise RuntimeError(f"onchainos {' '.join(args[:2])} error: {payload.get('error') or payload.get('data')}")
        return payload["data"]

    def wallet_address(self) -> str:
        if self.paper:
            return "PAPER_WALLET"
        status = self.run("wallet", "status")
        if not status.get("loggedIn"):
            raise RuntimeError("Agentic Wallet not logged in. Run: onchainos wallet login")
        addrs = self.run("wallet", "addresses", "--chain", CHAIN).get("solana", [])
        if not addrs:
            raise RuntimeError("No Solana address in the logged-in Agentic Wallet")
        return addrs[0]["address"]

    def balance(self, token_address: str) -> float:
        if self.paper:
            return self.state.paper_usdc if token_address == USDC_ADDRESS else self.state.paper_sol
        args = ["wallet", "balance", "--chain", CHAIN, "--force"]
        if token_address != NATIVE_SOL:
            args += ["--token-address", token_address]
        assets = self.run(*args)["details"][0].get("tokenAssets", [])
        for asset in assets:
            addr = asset.get("tokenAddress", "")
            is_native = token_address == NATIVE_SOL and addr in ("", NATIVE_SOL) and asset.get("symbol") == "SOL"
            if is_native or (token_address != NATIVE_SOL and addr == token_address):
                return float(asset.get("balance") or 0)
        return 0.0

    def bars(self, s: Strategy) -> list[dict[str, Any]]:
        """Oldest-first bars; `closed` is False for the still-forming bar."""
        if self.offline:
            return synthetic_bars(s)
        data = self.run("market", "kline", "--chain", CHAIN, "--address", WSOL_ADDRESS,
                        "--bar", s.timeframe, "--limit", str(KLINE_LIMIT))
        bars = [{"ts": int(b["ts"]) // 1000, "close": float(b["c"]), "closed": b.get("confirm") == "1"}
                for b in data]
        return sorted(bars, key=lambda b: b["ts"])

    def swap(self, from_token: str, to_token: str, amount: float, wallet: str, price: float) -> tuple[float, str]:
        """Swap `amount` (readable units) and return (amount received, tx hash)."""
        if self.paper:
            if from_token == USDC_ADDRESS:
                self.state.paper_usdc -= amount
                received = amount / price
                self.state.paper_sol += received
            else:
                self.state.paper_sol -= amount
                received = amount * price
                self.state.paper_usdc += received
            return received, "PAPER"
        before = self.balance(to_token)
        args = ["swap", "execute", "--chain", CHAIN, "--from", from_token, "--to", to_token,
                "--readable-amount", f"{amount:.9f}".rstrip("0").rstrip("."), "--wallet", wallet]
        if SLIPPAGE_PCT:
            args += ["--slippage", SLIPPAGE_PCT]
        result = self.run(*args)
        tx = result.get("swapTxHash", "")
        log(f"Swap broadcast: {tx}")
        for _ in range(12):  # broadcast != landed: wait for the balance to move
            time.sleep(5)
            received = self.balance(to_token) - before
            if received > 0:
                return received, tx
        raise RuntimeError(f"Swap {tx} broadcast but balance did not change after 60s; check the tx manually")


def synthetic_bars(s: Strategy) -> list[dict[str, Any]]:
    now = int(time.time()) // s.bar_seconds * s.bar_seconds
    bars = []
    for i in range(KLINE_LIMIT):
        close = 100 + (i % 20 - 10) * 0.2
        bars.append({"ts": now - (KLINE_LIMIT - 1 - i) * s.bar_seconds, "close": close, "closed": i < KLINE_LIMIT - 1})
    return bars


# --------------------------------------------------------------------------- trading


def open_position(api: OnchainOS, state: State, s: Strategy, wallet: str, price: float) -> bool:
    usdc = api.balance(USDC_ADDRESS)
    size = usdc * s.size_pct / 100
    if MAX_ORDER_USDC > 0:
        size = min(size, MAX_ORDER_USDC)
    if size < MIN_ORDER_USDC:
        log(f"Skip entry: order {size:.4f} USDC < MIN_ORDER_USDC {MIN_ORDER_USDC} (USDC balance {usdc:.4f})")
        return False
    notify(f"BUY signal at ${price:.4f}: swapping {size:.4f} USDC -> SOL")
    sol, tx = api.swap(USDC_ADDRESS, NATIVE_SOL, size, wallet, price)
    state.position = Position(entry_price=size / sol, size_sol=sol, cost_usdc=size,
                              entry_ts=int(time.time()), tx_hash=tx)
    state.trades_today += 1
    notify(f"Entry filled: {sol:.6f} SOL @ ${state.position.entry_price:.4f} (tx {tx})")
    return True


def adopt_sol_position(api: OnchainOS, state: State, price: float) -> bool:
    """Treat wallet SOL (minus gas/rent reserve) as an open position entered at the current price."""
    reserve = GAS_RESERVE_SOL + (USDC_ACCOUNT_RENT_SOL if api.balance(USDC_ADDRESS) <= 0 else 0)
    size = api.balance(NATIVE_SOL) - reserve
    if size * price < MIN_ORDER_USDC:
        return False
    state.position = Position(entry_price=price, size_sol=size, cost_usdc=size * price,
                              entry_ts=int(time.time()), tx_hash="ADOPTED")
    notify(f"Using {size:.6f} SOL from the wallet as an open position @ ${price:.4f} "
           f"(kept {reserve:.5f} SOL for fees)")
    return True


def close_position(api: OnchainOS, state: State, wallet: str, price: float, reason: str) -> None:
    pos = state.position
    assert pos is not None
    amount = min(pos.size_sol, api.balance(NATIVE_SOL) - GAS_RESERVE_SOL)
    if amount <= 0:
        raise RuntimeError(f"No SOL left to sell above the {GAS_RESERVE_SOL} SOL gas reserve")
    notify(f"EXIT {reason} at ${price:.4f}: swapping {amount:.6f} SOL -> USDC")
    usdc, tx = api.swap(NATIVE_SOL, USDC_ADDRESS, amount, wallet, price)
    pnl = usdc - pos.cost_usdc
    state.consecutive_losses = state.consecutive_losses + 1 if pnl < 0 else 0
    state.last_exit_ts = int(time.time())
    state.history.append({"entry": asdict(pos), "exit_ts": state.last_exit_ts, "exit_usdc": usdc,
                          "pnl_usdc": pnl, "reason": reason, "tx_hash": tx})
    state.position = None
    notify(f"Exit filled: {usdc:.4f} USDC, PnL {pnl:+.4f} USDC (tx {tx})")


def run_cycle(api: OnchainOS, state: State, s: Strategy, wallet: str, session_trades: int) -> int:
    now = time.time()
    state.roll_day(now)
    bars = api.bars(s)
    closed = [b for b in bars if b["closed"]]
    if len(closed) < s.slow_period + 2:
        raise RuntimeError(f"Not enough closed bars ({len(closed)})")
    price = bars[-1]["close"]
    closes = [b["close"] for b in closed]
    fast, slow = ema(closes, s.fast_period), ema(closes, s.slow_period)
    log(f"Price ${price:.4f} | EMA{s.fast_period} {fast[-1]:.4f} | EMA{s.slow_period} {slow[-1]:.4f}")
    RUNTIME.update(price=price, ema_fast=fast[-1], ema_slow=slow[-1], updated=int(now))

    if CLOSE_REQUESTED.is_set():
        CLOSE_REQUESTED.clear()
        if state.position:
            close_position(api, state, wallet, price, "MANUAL")
            return session_trades
        notify("/close ignored: no open position")

    if state.position:
        pos = state.position
        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
        log(f"Holding {pos.size_sol:.6f} SOL @ ${pos.entry_price:.4f} | PnL {pnl_pct:+.2f}%")
        reason = exit_reason(pos, price, now, s)
        if reason:
            close_position(api, state, wallet, price, reason)
        return session_trades

    if CAPITAL_ASSET == "SOL" and not state.paused and adopt_sol_position(api, state, price):
        return session_trades

    last_closed_ts = closed[-1]["ts"]
    if last_closed_ts == state.last_signal_bar_ts:
        return session_trades  # this bar was already evaluated
    state.last_signal_bar_ts = last_closed_ts
    if not crossed_above(fast, slow):
        log(f"No entry: waiting for EMA{s.fast_period} to cross above EMA{s.slow_period}")
        return session_trades
    blocked = entry_block_reason(state, s, session_trades, now)
    if blocked:
        log(f"BUY signal ignored: {blocked}")
        return session_trades
    if open_position(api, state, s, wallet, price):
        session_trades += 1
    return session_trades


HELP_TEXT = """Perintah:
/status - posisi, PnL, harga & EMA
/saldo - saldo SOL & USDC wallet bot
/history - 5 trade terakhir
/pause - stop entry baru (posisi tetap dijaga SL/TP)
/resume - lanjut entry
/close - jual posisi sekarang ke USDC
Selain perintah, ketik bebas untuk ngobrol dengan AI (kalau LLM_API_KEY di-set)."""


def status_text(state: State, s: Strategy, wallet: str, mode: str) -> str:
    price = RUNTIME.get("price")
    lines = [f"Mode: {mode} | wallet {wallet}",
             f"Strategi: {s.timeframe} EMA{s.fast_period}/{s.slow_period}, SL {s.stop_loss_pct}%, "
             f"TP {s.take_profit_pct}%, max hold {s.max_hold_bars} bar, size {s.size_pct}% USDC"]
    if price:
        updated = datetime.fromtimestamp(RUNTIME["updated"]).strftime("%H:%M:%S")
        lines.append(f"Harga SOL ${price:.4f} | EMA{s.fast_period} {RUNTIME['ema_fast']:.4f} | "
                     f"EMA{s.slow_period} {RUNTIME['ema_slow']:.4f} (update {updated})")
    pos = state.position
    if pos:
        held_h = (time.time() - pos.entry_ts) / 3600
        pnl = f" | PnL {(price - pos.entry_price) / pos.entry_price * 100:+.2f}%" if price else ""
        lines.append(f"Posisi: {pos.size_sol:.6f} SOL @ ${pos.entry_price:.4f}, {held_h:.1f} jam{pnl}")
    else:
        lines.append("Posisi: tidak ada")
    lines.append(f"Trade hari ini {state.trades_today}/{s.max_daily_trades} | rugi beruntun "
                 f"{state.consecutive_losses}/{s.max_consecutive_losses} | {'PAUSED' if state.paused else 'aktif'}")
    return "\n".join(lines)


def history_text(state: State, n: int = 5) -> str:
    if not state.history:
        return "Belum ada trade."
    rows = []
    for h in state.history[-n:]:
        ts = datetime.fromtimestamp(h["exit_ts"]).strftime("%m-%d %H:%M")
        rows.append(f"{ts} {h['reason']}: {h['entry']['size_sol']:.6f} SOL, PnL {h['pnl_usdc']:+.4f} USDC")
    return "\n".join(rows)


def tail_log(n: int = 15) -> str:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""


def handle_command(name: str, api: OnchainOS, state: State, s: Strategy, wallet: str, mode: str) -> str:
    if name in ("start", "help"):
        return HELP_TEXT
    if name == "status":
        return status_text(state, s, wallet, mode)
    if name in ("saldo", "balance"):
        return f"SOL: {api.balance(NATIVE_SOL):.6f}\nUSDC: {api.balance(USDC_ADDRESS):.4f}"
    if name == "history":
        return history_text(state)
    if name == "pause":
        state.paused = True
        return "Entry baru di-pause. Posisi yang ada tetap dijaga SL/TP/max hold."
    if name == "resume":
        state.paused = False
        return "Entry dilanjutkan."
    if name == "close":
        if not state.position:
            return "Tidak ada posisi terbuka."
        CLOSE_REQUESTED.set()
        WAKE.set()
        return "Oke, posisi akan dijual ke USDC di siklus berikutnya (beberapa detik)."
    return "Perintah tidak dikenal. Ketik /help."


def chat_reply(history: list[dict], state: State, s: Strategy, wallet: str, mode: str) -> str:
    system = (
        "Kamu asisten untuk bot trading SOL/USDC milik user (OKX Onchain OS, Solana). "
        "Jawab singkat, santai, pakai bahasa yang sama dengan user. Kamu TIDAK bisa mengeksekusi trade; "
        "kalau user mau aksi, sarankan perintah: /status /saldo /history /pause /resume /close. "
        "Jangan memberi janji profit. Data terkini bot:\n"
        f"{status_text(state, s, wallet, mode)}\n\nTrade terakhir:\n{history_text(state)}\n\n"
        "Aturan: entry saat EMA cepat cross ke atas EMA lambat di bar yang sudah close; exit di SL/TP/max hold; "
        f"cooldown {s.cooldown_bars} bar, max {s.max_daily_trades} entry/hari (UTC), pause setelah "
        f"{s.max_consecutive_losses} rugi beruntun. Kalau CAPITAL_ASSET=SOL, SOL di wallet dipakai sebagai posisi.\n\n"
        f"Log terakhir:\n{tail_log()}"
    )
    return ask_llm(LLM_API_KEY, system, history, LLM_MODEL) or "(AI tidak memberi jawaban)"


def start_telegram(api: OnchainOS, state: State, s: Strategy, wallet: str, mode: str) -> TelegramChat | None:
    if not TELEGRAM_BOT_TOKEN:
        return None
    on_text = (lambda _text, hist: chat_reply(hist, state, s, wallet, mode)) if LLM_API_KEY else None
    chat = TelegramChat(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                        on_command=lambda name, _args: handle_command(name, api, state, s, wallet, mode),
                        on_text=on_text, log=log)
    chat.start()
    log("Telegram listener started" + ("" if TELEGRAM_CHAT_ID else " (send /start to the bot to get your chat ID)"))
    return chat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SOL buy-dip bot (OKX Onchain OS)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Paper trading: real market data when onchainos is installed, simulated swaps")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args(argv)

    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    if not logger.handlers:
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    strategy = load_strategy()
    state_path = STATE_PATH + (".paper" if args.dry_run else "")
    state = load_state(state_path)
    api = OnchainOS(ONCHAINOS_BIN, paper=args.dry_run, state=state)

    if args.dry_run:
        log("Running in dry-run mode: swaps are simulated"
            + (" (onchainos not found, market data is synthetic)" if api.offline else ""))
    else:
        if not (os.path.isfile(ONCHAINOS_BIN) and os.access(ONCHAINOS_BIN, os.X_OK)):
            log(f"ERROR: onchainos not found at {ONCHAINOS_BIN}. Install it or set ONCHAINOS_BIN.")
            return 1
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log("WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; Telegram notifications disabled")

    try:
        wallet = api.wallet_address()
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    mode = "DRY-RUN" if args.dry_run else "LIVE TRADING"
    notify(f"SOL dip bot started ({mode}) | wallet {wallet} | {strategy.timeframe} "
           f"EMA{strategy.fast_period}/{strategy.slow_period} "
           f"SL {strategy.stop_loss_pct}% TP {strategy.take_profit_pct}%")
    if state.position:
        log(f"Resuming open position: {state.position.size_sol:.6f} SOL @ ${state.position.entry_price:.4f}")

    start_telegram(api, state, strategy, wallet, mode)

    session_trades = 0
    while True:
        try:
            session_trades = run_cycle(api, state, strategy, wallet, session_trades)
            save_state(state, state_path)
        except KeyboardInterrupt:
            log("Stopping bot.")
            break
        except Exception as exc:
            save_state(state, state_path)
            notify(f"ERROR: {exc}")
            if args.once:
                return 1
        if args.once:
            break
        try:
            WAKE.wait(POLL_SECONDS)
            WAKE.clear()
        except KeyboardInterrupt:
            log("Stopping bot.")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
