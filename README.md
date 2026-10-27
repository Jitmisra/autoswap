# autoswap

Moves Claude Code to a spare account before the active one hits its usage limit, in the middle of a session. macOS, Python 3 standard library only, one file (`autoswap.py`).

## How it decides

Two signals, both from Claude Code itself:

1. **The statusline.** Claude Code runs the `statusLine` command and passes it your usage (`rate_limits.five_hour` / `seven_day`). But a chat only refreshes that number when *that chat* gets a response, so a workflow running in an idle chat is invisible to it (it can show 5% while the account is at 20%). autoswap keeps the highest reading per window, since usage never falls inside a window.
2. **Polling `/usage`.** Every statusline run (every 60s via `refreshInterval`, even in idle chats) checks whether a poll is due and, if so, launches a detached `claude -p "/usage"`, Claude Code's own command, headless, hooks off, nothing persisted. It reports the real figure, which replaces the statusline's. Cadence: headroom to the line x 6 s, clamped to 2-10 minutes (far from the line = rare, near it = every 2 minutes). Each poll takes ~14 s and ~11 s of CPU.

When the active account reaches its line (default 85% for 5h, 95% for 7d), it moves to the spare account with the most room. Claude Code re-reads its credential within ~30s, so the line sits well below 100%.

## Setup (once)

Repeat the add step for each account: log in with `/login` inside `claude`, exit, then add it.

```bash
cd ~/Desktop/claudeswap/autoswap
python3 autoswap.py add a        # while Claude Code is logged in as account A
claude                           # /login as the next account, exit
python3 autoswap.py add b        # ...and so on for c, d, e
python3 autoswap.py add main --manual    # the account you switch by hand (see below)
python3 autoswap.py list         # all listed, * marks the active one
python3 autoswap.py snippet      # prints the statusLine entry for ~/.claude/settings.json
```

## The manual-only account

`add <label> --manual` (or `manual <label>` on an account you already added; `manual <label> off` undoes it) marks an account that autoswap never moves you onto or off:

- it is never picked as an automatic target, even when it has the most room;
- while you are on it, nothing switches automatically; the status line shows `⚠ near limit; switch manually` once it crosses a line;
- `autoswap switch <label>` still works to and from it, and its refreshed login is still saved so its token doesn't go stale.

With every automatic account over its line, autoswap holds still and says so rather than falling back to the manual one. When the active account is hard at 100%, the 10-minute cooldown is skipped so a spare that turned out to be exhausted doesn't strand the session.

The first `add` makes macOS ask whether `security` may read the "Claude Code-credentials" item. Choose Always Allow.

Try it without switching anything first: set `AUTOSWAP_DRY_RUN=1` in the statusLine command (`AUTOSWAP_DRY_RUN=1 python3 -I ... statusline`) and watch `~/.autoswap/autoswap.log` for `dry run: would switch`.

## Commands

| | |
|---|---|
| `add <label> [--manual]` | store the account Claude Code is logged into now |
| `manual <label> [on\|off]` | mark an account manual-only, or put it back in rotation |
| `list` | accounts and last-seen usage |
| `switch <label>` | switch now (manual) |
| `remove <label>` | forget an account (Claude's own login is untouched) |
| `statusline` | the Claude Code statusLine command |
| `snippet` | print the settings.json entry |

Tuning: `AUTOSWAP_THRESHOLD` (5h line, default 90), `AUTOSWAP_WEEKLY_THRESHOLD` (7d line, default 95). Cooldown between automatic switches is 10 minutes.

## What it touches

| | |
|---|---|
| Keychain `Claude Code-credentials` | written on a switch (this is how Claude Code gets the new login) |
| Keychain `autoswap-account` | one item per stored account (the only place secrets are kept) |
| `~/.claude.json` | only the `oauthAccount` key is replaced; atomic write, mode preserved |
| `~/.claude.lock`, `~/.claude/.oauth_refresh.lock`, `~/.claude.json.lock` | held briefly during a switch, the same locks Claude Code uses |
| `~/.autoswap/` (0700) | `state.json` (labels, emails, usage; no secrets), `autoswap.log` (no secrets) |

## Safety properties (enforced by `test_autoswap.py`)

- No network code. The test fails if the source mentions `urllib`, `socket`, `http`, `ssl`, `requests`, `asyncio`, etc.
- No token refresh. Claude Code refreshes its own tokens; autoswap only copies the refreshed login back into the account's slot when it leaves that account (and every 20 min).
- Only three process spawns exist (the test pins this): `/usr/bin/security` (absolute path), a detached copy of itself, and the `claude` executable running its own `/usage`.
- The `/usage` poll runs with `disableAllHooks` and `--no-session-persistence`, from an empty directory, so it doesn't fire your hooks or add entries to `claude --resume`.
- Secrets go to `security` over stdin. Account slots are small (~600 bytes) and are never allowed to fall back to argv. The one exception is writing Claude Code's own login item: it also holds every MCP server login (~20 KB on a typical machine), which cannot fit through `security -i`'s 4 KB stdin line, so that single write passes hex as an argument for the ~20 ms the process runs. Only processes running as you can see it (a tool that logs command lines, such as an EDR agent, would capture it).
- MCP logins and plugin secrets are machine-wide: they are never copied into account slots, and a switch keeps the live copies. Only the account's own fields (`claudeAiOauth`, `trustedDeviceToken`) are swapped.
- No `eval`/`exec`/`shell=True`/`pickle`/`base64`; imports are stdlib only.
- It refuses to switch if the live login isn't one it manages (so it can't silently discard a login), if `CLAUDE_CONFIG_DIR` is set, or if Claude Code holds its lock.
- Write-then-verify on every Keychain write; a failed switch restores the previous login.

## Run the tests

```bash
python3 -m unittest -v      # 76 tests, in-memory keychain + temp HOME, touches nothing real
```

## Known limits

- Between polls the figure can be up to ~10 minutes old when far from the line and ~2 minutes old near it, so the account can overshoot the line by a few percent. `autoswap threshold 80` gives more room; `autoswap poll` fetches the real figure on demand.
- Only the active account is measured. Other accounts show their last-seen figure and its age in `list`.
- If a Claude Code update changes the `/usage` text, polling fails safe: nothing is recorded, the log says so, and the statusline figure is used.
- Prompt cache is per account, so the first message after a switch is slower and uses more quota.
- If every account is over its line it holds still and the status line says so.
- Run `autoswap add` again for an account if you ever `/login` to it again outside autoswap.
