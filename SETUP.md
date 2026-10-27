# Setting up autoswap on your Mac

autoswap watches your Claude Code usage and, before the active account hits its limit, moves your session onto the next account that has room. This page takes you from nothing to working in about 10 minutes. It's per machine: each person runs it on their own Mac, and nothing here is shared between machines.

## Before you start

- **Use accounts you're entitled to.** Each Claude seat is for one person. autoswap rotates *your* machine through accounts *you* may use; it doesn't make sharing one seat between several people okay, so check with whoever administers your Claude org. Two people on one account also drain the same 5-hour limit, so the switch point will come sooner than either of you expects.
- macOS, with Claude Code installed and started at least once (`claude`).
- Python 3.9 or newer. The system `python3` is fine. Check with `python3 --version`.
- Your accounts' logins to hand: each `claude auth login` below opens a browser, and you sign in as that account.
- `CLAUDE_CONFIG_DIR` must not be set (autoswap only supports the default profile).

## 1. Get the code

```bash
git clone https://github.com/Jitmisra/autoswap.git ~/autoswap
cd ~/autoswap
python3 autoswap.py --help
```

Keep the folder where it is. The status line you install in step 4 points at this path. Nothing needs installing: it's one file and uses only the Python standard library.

## 2. Add your accounts, one at a time

Repeat this pair of commands for each account. Use any short label you like (`acc1`, `acc2`, ...).

```bash
claude auth login            # a browser opens: sign in as the account you want to add
python3 autoswap.py add acc1 # stores the account Claude Code is logged into right now
```

Then do it again for the next account:

```bash
claude auth login            # sign in as the NEXT account
python3 autoswap.py add acc2
```

Tips:

- **Check the browser page names the account you mean.** If your browser is already signed in to claude.ai it may pick that account for you. Use the account chooser, or sign out of claude.ai first, or use a private window.
- If you pick the same account twice, `add` refuses with `this login is already stored as 'acc1'`. Nothing is overwritten; just log in as the right account and run `add` again.
- **The first `add` makes macOS ask** whether `security` may read the "Claude Code-credentials" item. Choose **Always Allow**.
- **One account you only want to switch to by hand?** Add it with `--manual`:

  ```bash
  claude auth login
  python3 autoswap.py add main --manual
  ```

  autoswap never moves you onto or off a manual account. It's the one you switch to yourself (step 6).

When you're done, check that they're all there:

```bash
python3 autoswap.py list
```

`*` marks the account you're on now; manual accounts are tagged `[manual-only]`.

## 3. Choose when it switches

The default is 90% of the 5-hour limit, and 95% of the weekly limit where an account has one. A switch takes up to about 30 seconds to reach a running session, so a line well under 100% leaves room for that. We use 85:

```bash
python3 autoswap.py threshold 85
```

Allowed range is 50 to 99. Add `--weekly N` to set the weekly line. Running `threshold` with no number shows the current lines.

## 4. Turn it on

Open `~/.claude/settings.json` and add a `statusLine` entry. Get the exact command for your machine with:

```bash
python3 autoswap.py snippet
```

Copy the `command` it prints, and add `refreshInterval` so autoswap keeps checking even when a chat is idle:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/python3 -I /Users/you/autoswap/autoswap.py statusline",
    "refreshInterval": 60
  }
}
```

(Keep your other settings; this goes alongside them.)

- If you already have a `statusLine`, this replaces it. Copy `settings.json` somewhere first if you want to keep the old one.
- Start a **new** `claude` session afterwards. The status line now shows your account label and its usage, and autoswap runs every time it refreshes.

To try it without switching anything, put `AUTOSWAP_DRY_RUN=1` in front of the command in `settings.json` and watch `~/.autoswap/autoswap.log` for `dry run: would switch`. Remove it to go live.

## 5. What to expect

- On the active account, the status line shows its label and the 5-hour percentage. When it reaches the line, autoswap moves you to the spare with the most room and logs `switched A -> B (auto)`.
- Your running Claude Code session picks up the new account on its own within about 30 seconds. No restart is needed.
- The first message after a switch is a little slower and uses more quota, because prompt cache is per account.
- Right after a switch it won't switch again for 10 minutes (unless the account is at 100%).
- If every automatic account is over its line, autoswap stays put and the status line says `no spare account has room`.
- Only the account you're on is measured. The others show their last-seen figure and how old it is.

## 6. Everyday commands

Run these from `~/autoswap` (or use the full path to `autoswap.py`).

| Command | What it does |
|---|---|
| `python3 autoswap.py list` | every account, the last-seen usage and how old it is |
| `python3 autoswap.py poll` | ask Claude Code for the active account's real usage now |
| `python3 autoswap.py switch <label>` | switch now (works to and from a manual account) |
| `python3 autoswap.py threshold [N] [--weekly N]` | show or set the switch lines |
| `python3 autoswap.py manual <label> [on\|off]` | make an account manual-only, or put it back in rotation |
| `python3 autoswap.py remove <label>` | forget an account (Claude's own login is untouched) |

Handy shortcut in `~/.zshrc`:

```bash
alias autoswap='python3 ~/autoswap/autoswap.py'
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Claude Code is not logged in` | Run `claude auth login`, then `add` again. |
| `this login is already stored as ...` | The browser logged in as an account you already added. Sign in as a different one. |
| `... holds a different account; pick another label` | That label is taken by another account. Pick a new label (or `--force` to replace it on purpose). |
| `CLAUDE_CONFIG_DIR is set` | Unset it. autoswap only supports the default profile. |
| Status line says `autoswap: login not added` | You're logged in as an account that isn't stored. Run `add <label>`. |
| Status line says `autoswap: error` | See `~/.autoswap/autoswap.log` for the reason. |
| A switch is refused because Claude Code holds its lock | Harmless; it retries on the next refresh. |
| Usage never updates in `list` | Run `python3 autoswap.py poll`; the log says why if it fails. It needs the `claude` executable to be findable. |
| You logged into an account again outside autoswap | Run `add <label>` for it again so its stored login isn't stale. |

## Uninstall

1. Remove the `statusLine` entry from `~/.claude/settings.json`.
2. For each label: `python3 autoswap.py remove <label>`.
3. `rm -rf ~/.autoswap` (state, config and log; it contains no secrets).
4. Delete the `~/autoswap` folder.

Removing accounts never logs you out of Claude Code itself.

## Safe to leave running?

autoswap has no network code and never refreshes tokens; Claude Code does that itself. Everything it keeps is in your macOS Keychain and `~/.autoswap/`, on your machine only. The README lists exactly what it touches and what the tests enforce.
