#!/usr/bin/env python3
"""SOL buy-dip bot with LIVE trading execution via OnchainOS swap."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from datetime import datetime

CHAIN = "solana"
TOKEN_ADDRESS = "So11111111111111111111111111111111111111112"  # wSOL
USDC_ADDRESS = "EPjFWaLb3hyccqJ1xNg5Bf4xAgG5PhtzQdnGA3XcjT1d"  # USDC on Solana
TIMEFRAME = "5m"
BARS = 50
POSITION_SIZE_SOL = 0.0002  # Start small for live
STOP_LOSS_PCT = 1.8
TAKE_PROFIT_PCT = 5.0
MAX_HOLD_BARS = 144  # 12 hours in 5m bars
SLIPPAGE_PCT = 1.0

# Runtime flags (can be toggled via CLI args)
DRY_RUN = False
ONCE = False

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


@dataclass
class Position:
    entry_price: float
    entry_bar: int
    size_sol: float
    size_usdc: float
    entry_time: str
    tx_hash: str = ""
    active: bool = True


def log(msg: str) -> None:
    """Log with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
        }
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (HTTPError, URLError, Exception) as exc:
        log(f"Telegram notify failed: {exc}")


def notify(msg: str) -> None:
    log(msg)
    send_telegram_message(msg)


def run_cmd(args: list[str]) -> dict[str, Any]:
    # When running in dry-run mode, simulate common onchainos responses so
    # the bot can be tested without external dependencies.
    if DRY_RUN:
        cmd = args[1:] if len(args) > 1 else args
        # Wallet balance
        if "wallet" in cmd:
            return {
                "data": {
                    "details": [
                        {
                            "tokenAssets": [
                                {"address": "FakeWalletAddress", "symbol": "SOL", "balance": "1.234"}
                            ]
                        }
                    ]
                }
            }
        # Market kline
        if "market" in cmd and "kline" in cmd:
            now = int(time.time())
            bars = []
            for i in range(BARS):
                ts = now - (BARS - i) * 300
                o = 20.0 + (i * 0.01)
                c = o + (0.005 if i % 2 == 0 else -0.005)
                bars.append({"ts": ts, "o": o, "h": o + 0.01, "l": o - 0.01, "c": c, "volUsd": 1000 + i})
            return {"data": list(reversed(bars))}
        # Swap quote
        if "swap" in cmd and "quote" in cmd:
            # read amount parameter if present
            try:
                idx = args.index("--amount")
                amt = int(args[idx + 1])
            except Exception:
                amt = 0
            # If amount likely lamports (SOL), convert to SOL for quote
            # assume lamports if amt > 1e6
            sol_amount = float(amt) / 1e9 if amt > 1e6 else float(amt) / 1e6
            # fake price: 1 SOL = 20 USDC
            usdc = sol_amount * 20
            output_amount = int(usdc * 1e6)
            return {"ok": True, "data": {"routes": [{"outputAmount": output_amount}]}}
        # Swap execute
        if "swap" in cmd and "execute" in cmd:
            try:
                idx = args.index("--amount")
                amt = int(args[idx + 1])
            except Exception:
                amt = 0
            sol_amount = float(amt) / 1e9 if amt > 1e6 else float(amt) / 1e6
            usdc = sol_amount * 20
            return {"ok": True, "data": {"txHash": "0xdeadbeef", "outputAmount": int(usdc * 1e6)}}
        # default simulated response
        return {"data": {}}

    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def get_wallet_address() -> str:
    """Get the Solana wallet address from Agentic Wallet."""
    data = run_cmd(["/home/codespace/.local/bin/onchainos", "wallet", "balance", "--chain", "solana"])
    details = data.get("data", {}).get("details", [])
    if not details:
        return ""
    token_assets = details[0].get("tokenAssets", [])
    for asset in token_assets:
        address = asset.get("address")
        if address:
            return address
    return ""


def get_balance() -> float:
    """Get current SOL balance in the wallet."""
    data = run_cmd(["/home/codespace/.local/bin/onchainos", "wallet", "balance", "--chain", "solana"])
    assets = data.get("data", {}).get("details", [{}])[0].get("tokenAssets", [])
    for asset in assets:
        if asset.get("symbol") == "SOL":
            return float(asset.get("balance", 0))
    return 0.0


def swap_sol_to_usdc(amount_sol: float, wallet: str) -> dict[str, Any] | None:
    """Execute SOL -> USDC swap via onchainos."""
    try:
        notify(f"Initiating swap: {amount_sol} SOL to USDC")
        
        # Get quote first
        quote_args = [
            "/home/codespace/.local/bin/onchainos",
            "swap",
            "quote",
            "--chain", "solana",
            "--from-token", TOKEN_ADDRESS,
            "--to-token", USDC_ADDRESS,
            "--amount", str(int(amount_sol * 1e9)),  # Convert to lamports
            "--slippage", str(SLIPPAGE_PCT),
        ]
        quote = run_cmd(quote_args)
        
        if quote.get("ok"):
            routes = quote.get("data", {}).get("routes", [])
            if not routes:
                log("ERROR: No swap routes available")
                return None
            
            log(f"Quote OK: {routes[0].get('outputAmount', 0)} USDC received")
            
            # Execute swap
            exec_args = [
                "/home/codespace/.local/bin/onchainos",
                "swap",
                "execute",
                "--chain", "solana",
                "--from-token", TOKEN_ADDRESS,
                "--to-token", USDC_ADDRESS,
                "--amount", str(int(amount_sol * 1e9)),
                "--slippage", str(SLIPPAGE_PCT),
            ]
            result = run_cmd(exec_args)
            
            if result.get("ok"):
                log(f"SWAP SUCCESS: tx {result.get('data', {}).get('txHash', 'unknown')}")
                return result.get("data", {})
            else:
                log(f"SWAP FAILED: {result.get('error', 'unknown error')}")
                return None
    except Exception as e:
        log(f"Swap execution error: {e}")
        return None


def swap_usdc_to_sol(amount_usdc: float, wallet: str) -> dict[str, Any] | None:
    """Execute USDC -> SOL swap via onchainos."""
    try:
        notify(f"Initiating exit swap: {amount_usdc} USDC to SOL")
        
        quote_args = [
            "/home/codespace/.local/bin/onchainos",
            "swap",
            "quote",
            "--chain", "solana",
            "--from-token", USDC_ADDRESS,
            "--to-token", TOKEN_ADDRESS,
            "--amount", str(int(amount_usdc * 1e6)),  # USDC is 6 decimals
            "--slippage", str(SLIPPAGE_PCT),
        ]
        quote = run_cmd(quote_args)
        
        if quote.get("ok"):
            routes = quote.get("data", {}).get("routes", [])
            if not routes:
                log("ERROR: No exit swap routes available")
                return None
            
            log(f"Exit quote OK: {routes[0].get('outputAmount', 0)} SOL received")
            
            exec_args = [
                "/home/codespace/.local/bin/onchainos",
                "swap",
                "execute",
                "--chain", "solana",
                "--from-token", USDC_ADDRESS,
                "--to-token", TOKEN_ADDRESS,
                "--amount", str(int(amount_usdc * 1e6)),
                "--slippage", str(SLIPPAGE_PCT),
            ]
            result = run_cmd(exec_args)
            
            if result.get("ok"):
                log(f"EXIT SWAP SUCCESS: tx {result.get('data', {}).get('txHash', 'unknown')}")
                return result.get("data", {})
            else:
                log(f"EXIT SWAP FAILED: {result.get('error', 'unknown error')}")
                return None
    except Exception as e:
        log(f"Exit swap error: {e}")
        return None


def fetch_bars() -> list[dict[str, Any]]:
    data = run_cmd([
        "/home/codespace/.local/bin/onchainos",
        "market",
        "kline",
        "--chain",
        CHAIN,
        "--address",
        TOKEN_ADDRESS,
        "--bar",
        TIMEFRAME,
        "--limit",
        str(BARS),
    ])
    bars = []
    for item in reversed(data["data"]):
        bars.append({
            "ts": int(item["ts"]),
            "open": float(item["o"]),
            "high": float(item["h"]),
            "low": float(item["l"]),
            "close": float(item["c"]),
            "volume": float(item.get("volUsd") or item.get("vol") or 0.0),
        })
    return bars


def ema(series: list[float], period: int) -> list[float]:
    if len(series) < period:
        return [0.0] * len(series)
    k = 2 / (period + 1)
    result = [series[0]]
    for value in series[1:]:
        result.append(value * k + result[-1] * (1 - k))
    return result


def get_price_info() -> dict[str, Any]:
    data = run_cmd([
        "/home/codespace/.local/bin/onchainos",
        "token",
        "price-info",
        "--chain",
        CHAIN,
        "--address",
        TOKEN_ADDRESS,
    ])
    return data.get("data", {})


def print_summary(bars: list[dict[str, Any]], ema5: list[float], ema10: list[float]) -> None:
    close = bars[-1]["close"]
    print("--- SOL Dip Bot Status ---")
    print(f"Latest close: ${close:.4f}")
    print(f"EMA5: {ema5[-1]:.4f}, EMA10: {ema10[-1]:.4f}")
    print(f"Latest bar ts: {bars[-1]['ts']}")


def main() -> None:
    notify("SOL dip bot — LIVE TRADING MODE\nStarting live trading with minimum capital protection")
    
    wallet = get_wallet_address()
    if not wallet:
        log("ERROR: Could not get wallet address. Exiting.")
        sys.exit(1)
    
    log(f"Wallet: {wallet}")
    position: Position | None = None
    session_trades = 0
    session_start = time.time()

    while True:
        try:
            bars = fetch_bars()
            closes = [bar["close"] for bar in bars]
            ema5 = ema(closes, 5)
            ema10 = ema(closes, 10)
            
            # Log status
            close = bars[-1]["close"]
            log(f"Close: ${close:.4f} | EMA5: {ema5[-1]:.4f} | EMA10: {ema10[-1]:.4f}")

            # Buy signal: EMA5 crosses above EMA10
            buy_signal = False
            if len(ema5) >= 2 and len(ema10) >= 2:
                if ema5[-2] < ema10[-2] and ema5[-1] > ema10[-1]:
                    buy_signal = True

            # Position management
            if position and position.active:
                current_price = closes[-1]
                pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
                bars_held = len(bars) - position.entry_bar
                
                log(f"Position: {position.size_sol} SOL @ ${position.entry_price:.4f}, PnL: {pnl_pct:.2f}%")
                
                exit_reason = None
                if pnl_pct <= -STOP_LOSS_PCT:
                    exit_reason = "STOP_LOSS"
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    exit_reason = "TAKE_PROFIT"
                elif bars_held >= MAX_HOLD_BARS:
                    exit_reason = "MAX_HOLD_TIME"
                
                if exit_reason:
                    notify(f"EXIT SIGNAL: {exit_reason} (PnL: {pnl_pct:.2f}%)")
                    
                    # Calculate USDC to convert back
                    exit_usdc = position.size_usdc
                    result = swap_usdc_to_sol(exit_usdc, wallet)
                    
                    if result:
                        position.active = False
                        session_trades += 1
                        notify(f"Trade #{session_trades} closed | Reason: {exit_reason}")
                    else:
                        notify("Exit swap failed, will retry next cycle")
                else:
                    log("Holding position...")
                    
            elif buy_signal:
                current_price = closes[-1]
                log(f"BUY SIGNAL: EMA5 crossed above EMA10 at ${current_price:.4f}")
                
                bal = get_balance()
                if bal < POSITION_SIZE_SOL:
                    log(f"ERROR: Insufficient SOL balance ({bal} < {POSITION_SIZE_SOL}). Cannot enter.")
                else:
                    log(f"Current balance: {bal} SOL")
                    log(f"Entering {POSITION_SIZE_SOL} SOL...")
                    
                    # Swap SOL -> USDC (buying at market)
                    result = swap_sol_to_usdc(POSITION_SIZE_SOL, wallet)
                    
                    if result:
                        usdc_received = float(result.get("outputAmount", 0)) / 1e6
                        position = Position(
                            entry_price=current_price,
                            entry_bar=len(bars) - 1,
                            size_sol=POSITION_SIZE_SOL,
                            size_usdc=usdc_received,
                            entry_time=datetime.now().isoformat(),
                            tx_hash=result.get("txHash", ""),
                        )
                        notify(f"Entry executed: {POSITION_SIZE_SOL} SOL into {usdc_received:.4f} USDC")
                        session_trades += 1
                    else:
                        notify("Entry swap failed. Waiting for next signal.")
            else:
                log("Waiting for BUY signal (EMA5 > EMA10)...")

            elapsed = int(time.time() - session_start)
            log(f"Session: {session_trades} trades | {elapsed}s elapsed")
            log("Next check in 5 minutes. Press Ctrl+C to stop.\n")
            # If running a single-cycle test, skip sleeping and break.
            if ONCE:
                break
            time.sleep(300)
            
        except KeyboardInterrupt:
            log("Stopping bot.")
            break
        except Exception as exc:
            log(f"ERROR: {exc}")
            log("Retrying in 60 seconds...")
            time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SOL dip bot")
    parser.add_argument("--dry-run", action="store_true", help="Run without external onchainos calls (simulate)")
    parser.add_argument("--once", action="store_true", help="Run a single main loop iteration and exit")
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    ONCE = args.once
    if DRY_RUN:
        log("Running in dry-run mode: external calls will be simulated")
    if ONCE:
        log("Running single-cycle mode (--once)")
    main()
