# Verified Agent Identity

A decentralized identity management toolkit for AI agents using iden3 protocol on Billions Network.

## Overview

This skill enables AI agents to create, manage, link, prove and verify ownership of decentralized identities (DIDs) using cryptographic signatures.

## Installation

### Agent CTA:

1. Ask bot to install the skill:

   ```plaintext
   Install the skill `npx clawhub@latest install verified-agent-identity`
   ```

   Or

   ```plaintext
   Install the `verified-agent-identity` skill from the OpenClaw clawhub.ai.
   ```

2. (Optional) If the verification process did not start automatically after installation, ask your bot to initialize the process by sending a message like:

   ```plaintext
   Please link your agent identity to me.
   ```

### Human CTA:

1. Install the skill:
   ```bash
   npx clawhub@latest install verified-agent-identity
   ```
2. Create a new identity:

   ```bash
   # Generate a new key and create a new identity
   node scripts/createNewEthereumIdentity.js
   ```

   Or

   ```bash
   # Use an existing private key to create an identity
   node scripts/createNewEthereumIdentity.js --key <your-ethereum-private-key>
   ```

3. Generate a verification link to connect your human identity to the agent:

   ```bash
   node scripts/manualLinkHumanToAgent.js --challenge '{"name": "Agent Name", "description": "Short description of the agent"}'
   ```

   This prints the verification URL to the console. Open it in your browser to complete the identity linking process.

## Features

- **Identity Creation**: Generate new DIDs with random or existing Ethereum private keys
- **Identity Management**: List and manage multiple identities with default identity support
- **Human-Agent Linking**: Link a human identity to an agent's DID through signed challenges
- **Proof Generation**: Generate cryptographic proofs to authenticate as a specific identity
- **Proof Verification**: Verify proofs to confirm identity ownership

## Architecture

### Runtime Requirements

- **Node.js `>= v20`** and **npm** are required to run the scripts.
- The **`openclaw` CLI** must be installed and available in `PATH`. It is a hard runtime dependency used exclusively for sending direct messages to other agents or users on the Billions Network.

### Dependency Surface

npm dependencies are intentionally minimal and scoped to well-established, audited packages:

| Package                | Purpose                                                      |
| ---------------------- | ------------------------------------------------------------ |
| `@0xpolygonid/js-sdk`  | iden3/Polygon ID cryptographic primitives and key management |
| `@iden3/js-iden3-core` | DID and identity core types                                  |
| `@iden3/js-iden3-auth` | JWS/JWA authorization response construction and verification |
| `ethers`               | Ethereum key utilities                                       |
| `shell-quote`          | Shell token parsing used **only** for input sanitization     |
| `uuid`                 | UUID generation for protocol message IDs                     |

### Key Storage and Isolation

All cryptographic material is persisted to `$HOME/.openclaw/billions/` — a directory that lives **outside the agent's workspace**:

| File               | Contents                                        |
| ------------------ | ----------------------------------------------- |
| `kms.json`         | Private keys (unencrypted, owner-readable only) |
| `identities.json`  | Identity metadata                               |
| `defaultDid.json`  | Active DID and associated public key            |
| `challenges.json`  | Per-DID challenge history                       |
| `credentials.json` | Verifiable credentials                          |

### Subprocess Execution Safety

**Only one specific command is ever executed: `openclaw message send`**, with a fixed, hardcoded argument structure. The binary name, the subcommand, and all flag names (`--target`, `--message`) are hardcoded. User-supplied values are only ever passed as the **values** of those flags, never as the command name, subcommand, or flag names. Nothing else can be executed. There is no mechanism to change the binary, add flags, or inject subcommands. Additional security properties:

1. **No shell interpolation**: `execFileSync(binary, argsArray)` bypasses the OS shell entirely. The OS `exec*` syscall receives the argument vector directly — shell metacharacters in argument values are treated as literal data.
2. **Argument-level input validation**: Before the call, all user-supplied values pass through two independent validation layers:
   - `validateTarget` — enforces a strict allowlist regex (`/^[A-Za-z0-9:._@\-\/]+$/`) on the `--target` value.
   - `assertNoShellOperators` — uses `shell-quote` to tokenize the input and rejects any token with an `op` property (i.e., `|`, `&`, `;`, `>`, `<`, `$()`, etc.).
3. **No dynamic code execution**: No `eval`, `new Function`, `child_process.exec`, or shell-interpolated `execSync` calls exist anywhere in the codebase.

Prompt injection and arbitrary code execution are structurally impossible: the executed command and its flags are hardcoded constants, and user data can only influence the string values passed to `--target` and `--message` after sanitization.

### Network and External Binary Policy

- All external http calls will be made to trusted resources.
- No external binary other than `openclaw` is invoked.
- Any external URLs or verification links produced by the scripts are delivered to the user as a plain text message via `openclaw message send`. The agent has no ability to follow, fetch, open, or interact with those URLs in any way - it only forwards the string to the user.

## Documentation

See [SKILL.md](SKILL.md) for detailed usage instructions and examples.

## SOL Dip Bot — quick usage

`sol_dip_bot.py` trades SOL/USDC on Solana through the official OKX Onchain OS CLI
(`onchainos`) and its Agentic Wallet. Parameters come from `sol_strategy.json`.

- Starting capital can be SOL (`CAPITAL_ASSET=SOL`, default): when the bot has no position, wallet SOL
  (minus a gas reserve, plus USDC account rent on first use) is treated as an open position entered at
  the current price. No swap happens until an exit sells it to USDC.
- Entry: when EMA5 crosses above EMA10 on closed 5m bars, buy SOL with `SIZE_PCT`% (default 10%) of the wallet's USDC.
- Exit: sell the SOL back to USDC on -1.8% stop loss, +5% take profit, or after 144 bars (12h).
- Risk overlays: max 1 entry per UTC day, 2-bar cooldown after an exit, pause after 2
  consecutive losses (until UTC 00:00), and `MAX_TRADES_PER_SESSION`.
- Open position, daily counters, and trade history persist in `sol_dip_bot_state.json`,
  so a restart resumes an open position.

### Setup

```bash
npx -y @okxweb3/onchainos-installer install          # installs onchainos CLI
npx -y skills add okx/onchainos-skills --skill '*' -a universal -y   # official OKX agent skills (.agents/skills)
onchainos wallet login                              # log in to the Agentic Wallet
onchainos wallet addresses --chain solana           # fund this address with SOL (or USDC + a little SOL for fees)
```

Set per-transaction and daily limits in Agentic Wallet Policy Settings before running live.

### Run

```bash
python3 sol_dip_bot.py --dry-run --once   # one paper cycle (real market data, simulated swaps)
python3 sol_dip_bot.py --dry-run          # continuous paper trading
python3 sol_dip_bot.py                    # LIVE trading
./start_sol_dip_bot.sh                    # LIVE trading in the background (nohup)
```

Dry-run uses real market data when `onchainos` is installed and synthetic data otherwise.
Paper balances start at `PAPER_USDC` and persist in `sol_dip_bot_state.json.paper`.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONCHAINOS_BIN` | `onchainos` on `PATH`, else `~/.local/bin/onchainos` | CLI path |
| `MAX_TRADES_PER_SESSION` | `10` | Max entries per process run |
| `CAPITAL_ASSET` | `SOL` | `SOL` adopts wallet SOL as a position when flat; `USDC` only trades USDC |
| `SIZE_PCT` | `sizing.pct` in `sol_strategy.json` (10) | % of USDC per entry; use `100` for tiny wallets |
| `GAS_RESERVE_SOL` | `0.001` | SOL never traded, kept for fees |
| `MIN_ORDER_USDC` | `0.1` | Skip entries/adoption smaller than this |
| `MAX_ORDER_USDC` | `0` (no cap) | Cap on a single entry |
| `SLIPPAGE_PCT` | `1` | Swap slippage %; empty = CLI auto slippage |
| `POLL_SECONDS` | `60` | Seconds between cycles |
| `PAPER_USDC`, `PAPER_SOL` | `100`, `0` | Starting paper balances for dry-run |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | unset | Optional Telegram notifications |

| `LLM_API_KEY` | unset | Telegram AI chat key: Gemini `AIza...`/`AQ....` (free, aistudio.google.com/apikey), Groq `gsk_...` (free), OpenAI `sk-...`, or Anthropic `sk-ant-...` |
| `LLM_MODEL` | `gemini-flash-lite-latest` / `llama-3.3-70b-versatile` / `gpt-4o-mini` / `claude-3-5-haiku-latest` | Override the chat model |

Logs: stdout and `sol_dip_bot.log` (rotating, 1MB x 3).

### Telegram control and AI chat

With `TELEGRAM_BOT_TOKEN` set, the bot listens to Telegram (long polling, no webhook needed).
It answers only `TELEGRAM_CHAT_ID`. If the chat ID is unset, send `/start` to the bot and it replies with your chat ID.

| Command | Action |
| --- | --- |
| `/status` | Position, PnL, price, EMAs, limits |
| `/saldo` | Wallet SOL and USDC |
| `/history` | Last 5 trades |
| `/pause`, `/resume` | Stop or resume new entries (open positions keep SL/TP/max hold) |
| `/close` | Sell the open position to USDC now |

Any other text goes to the LLM in `LLM_API_KEY`. The bot sends its status, recent trades, and recent log lines as context.
The AI only answers questions. It cannot trade.

## Chat with Devin from Telegram (`devin_telegram_bridge.py`)

The bridge forwards your Telegram messages to a Devin session and sends Devin's replies back to Telegram.
It is unofficial, and every session uses Devin ACUs.

1. Create a service user API key (`cog_...`) in Devin under **Settings > Devin API > Service users**.
2. Run the bridge on a host that stays on (VPS or server):

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... DEVIN_API_KEY=cog_... DEVIN_ORG_ID=org-...
# optional: DEVIN_SESSION_ID=devin-... to continue an existing session, DEVIN_MAX_ACU=10 to cap new sessions
nohup python3 devin_telegram_bridge.py > devin_bridge.out 2>&1 &
```

Only one process can poll a Telegram bot. When the bridge runs, start the trading bot with `TELEGRAM_LISTEN=0`.
The bot still sends trade notifications.
Telegram commands: `/new <message>` starts a new session and `/session` shows the current session link.
