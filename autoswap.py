#!/usr/bin/env python3
"""autoswap - move Claude Code to a spare account before its usage limit hits (macOS).

How it works
  Claude Code runs the `statusline` command after every assistant message and
  hands it the subscription usage (rate_limits.five_hour / seven_day). When the
  active account crosses its line, a detached `switch` swaps the spare
  account's stored login into Claude Code's own credential slot. The running
  session picks it up on its next credential read (Claude caches it ~30s), so
  the line is deliberately below 100%.

What it does not do
  No network access of any kind, no token refresh, no daemon, no third-party
  packages. Secrets live only in the login Keychain and travel over stdin to
  /usr/bin/security. The one exception is Claude Code's own login item, which
  also holds every MCP server login and outgrows the stdin buffer; writing it
  passes hex as an argument for the ~20ms the process runs. Claude Code
  refreshes tokens itself; we only copy the refreshed login back into the
  account's slot when we leave it, and never touch the shared MCP logins.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SECURITY = "/usr/bin/security"
LIVE_SERVICE = "Claude Code-credentials"
SLOT_SERVICE = "autoswap-account"
NOT_FOUND_RC = 44
KEYCHAIN_TIMEOUT_S = 5.0
# `security -i` reads one stdin line into a 4096-byte buffer; longer is truncated.
STDIN_LINE_LIMIT = 4096 - 64

COOLDOWN_S = 600  # minimum gap between automatic switches
SETTLE_S = 90  # ignore usage readings this long after a switch (they predate it)
PENDING_S = 30  # don't respawn a switch while one was launched this recently
SYNC_EVERY_S = 1200  # copy Claude's refreshed login back into the active slot
CRED_LOCK_STALE_S = 60.0  # Claude Code's own staleness for its credential locks
CONFIG_LOCK_STALE_S = 10.0
LOCK_TIMEOUT_S = 9.0

LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
WINDOWS = ("five_hour", "seven_day")
SHORT = {"five_hour": "5h", "seven_day": "7d"}
UNKNOWN_SCORE = 30.0  # how full an account with no reading yet is ranked as
SAME_WINDOW_S = 300  # two resets_at this close are the same usage window
# The statusline only refreshes in a chat when *that chat* gets a response, so a
# workflow running in an idle chat is invisible to it. Polling asks Claude Code's
# own `/usage` for the real figure, as often as the distance to the line demands.
POLL_MIN_S = 120.0
POLL_MAX_S = 600.0
POLL_S_PER_PCT = 6.0  # seconds of polling interval per percentage point of headroom
POLL_PENDING_S = 120.0  # don't launch another poll while one was launched this recently
POLL_TIMEOUT_S = 90.0


class SwitchError(Exception):
    """A refusal or failure with a message that is safe to show the user."""


class KeychainError(SwitchError):
    pass


class LockTimeout(SwitchError):
    pass


def _setting(env: str, key: str, default: float) -> float:
    """Environment variable first, then ~/.autoswap/config.json, then the default."""
    for raw in (os.environ.get(env), _read_config().get(key)):
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return default


def _read_config() -> dict:
    try:
        data = json.loads((state_dir() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def threshold() -> float:
    """Switch away when the 5h window reaches this percent."""
    return _setting("AUTOSWAP_THRESHOLD", "threshold", 90.0)


def weekly_threshold() -> float:
    """Switch away when the 7d window reaches this percent."""
    return _setting("AUTOSWAP_WEEKLY_THRESHOLD", "weeklyThreshold", 95.0)


# ---------------------------------------------------------------- paths / files


def state_dir() -> Path:
    return Path.home() / ".autoswap"


def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def claude_home() -> Path:
    return Path.home() / ".claude"


def check_environment() -> None:
    # A custom config dir means Claude uses a different credential store than
    # the one we would write, so a switch would silently do nothing.
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        if os.environ.get(var) is not None:
            raise SwitchError(f"{var} is set; autoswap only supports the default profile")


def ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def log(msg: str) -> None:
    """Append one line to the log. Callers must never pass credential material."""
    try:
        p = ensure_state_dir() / "autoswap.log"
        if p.exists() and p.stat().st_size > 512 * 1024:
            os.replace(p, p.with_name("autoswap.log.1"))
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------- locking


@contextmanager
def file_lock(path: Path, timeout_s: float):
    """Exclusive flock between autoswap processes (released on close/exit)."""
    ensure_state_dir()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"timed out waiting for {path.name}")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def dir_lock(path: Path, stale_s: float, timeout_s: float | None = None):
    """Claude Code's lock protocol (npm proper-lockfile): the lock is a directory
    and mkdir is the mutex. A lock older than ``stale_s`` has a dead holder."""
    deadline = time.monotonic() + (LOCK_TIMEOUT_S if timeout_s is None else timeout_s)
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_s:
                grave = path.with_name(f"{path.name}.stale-{os.getpid()}")
                try:
                    os.rename(path, grave)
                    os.rmdir(grave)
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise LockTimeout(f"{path.name} is held (Claude Code mid-refresh?); try again")
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            path.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------- keychain


def _quote(value: str) -> str:
    """Quote for a `security -i` stdin line, which is re-parsed shell-style."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [SECURITY, *args], input=stdin, capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise KeychainError(f"security CLI unavailable: {type(e).__name__}") from e


class SecurityKeychain:
    """The macOS login Keychain through Apple's /usr/bin/security (absolute path,
    so nothing earlier on PATH can intercept a secret)."""

    def get(self, service: str, account: str) -> str | None:
        r = _security(["find-generic-password", "-a", account, "-s", service, "-w"])
        if r.returncode == 0:
            return r.stdout.removesuffix("\n")
        if r.returncode == NOT_FOUND_RC:
            return None
        raise KeychainError(f"keychain read failed (rc={r.returncode})")

    def set(self, service: str, account: str, secret: str, allow_argv: bool = False) -> None:
        hex_value = secret.encode("utf-8").hex()
        line = f"add-generic-password -U -a {_quote(account)} -s {_quote(service)} -X {hex_value}\n"
        if len(line.encode("utf-8")) <= STDIN_LINE_LIMIT:
            r = _security(["-i"], stdin=line)
        elif allow_argv:
            # Only Claude Code's own login item may take this path (see write_live):
            # it also holds every MCP server login, so it outgrows the stdin buffer.
            r = _security(["add-generic-password", "-U", "-a", account, "-s", service, "-X", hex_value])
        else:
            raise KeychainError("credential too large for `security -i`; refusing to use argv")
        if r.returncode != 0:
            raise KeychainError(f"keychain write failed (rc={r.returncode})")

    def delete(self, service: str, account: str) -> None:
        r = _security(["delete-generic-password", "-a", account, "-s", service])
        if r.returncode not in (0, NOT_FOUND_RC):
            raise KeychainError(f"keychain delete failed (rc={r.returncode})")


KEYCHAIN = SecurityKeychain()


def live_account_name() -> str:
    """The Keychain account name Claude Code uses for its own item."""
    user = os.environ.get("USER")
    if user:
        return user
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def read_live_credentials() -> str | None:
    return KEYCHAIN.get(LIVE_SERVICE, live_account_name())


def write_verified(service: str, account: str, secret: str, allow_argv: bool = False) -> None:
    """Write then read back: `security -i` can exit 0 on a failed command."""
    KEYCHAIN.set(service, account, secret, allow_argv=allow_argv)
    if KEYCHAIN.get(service, account) != secret:
        raise KeychainError("keychain write did not verify")


# Claude Code keeps machine-wide integrations (MCP server logins, plugin secrets)
# in the same Keychain item as the account login. They rotate independently of
# any account, so the live copy is authoritative and must survive a switch;
# everything else (claudeAiOauth, trustedDeviceToken, ...) belongs to the account.
SHARED_KEYS = frozenset({"mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets"})


def _compact(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def account_part(blob: str) -> str:
    """The account-owned fields of a credential blob: what a slot stores."""
    return _compact({k: v for k, v in json.loads(blob).items() if k not in SHARED_KEYS})


def compose_live(slot_blob: str, live_blob: str | None) -> str:
    """The login to activate: the slot's account fields plus the live shared fields."""
    merged = json.loads(slot_blob)
    try:
        live = json.loads(live_blob or "{}")
    except ValueError:
        live = {}
    if isinstance(live, dict):
        merged.update({k: v for k, v in live.items() if k in SHARED_KEYS})
    return _compact(merged)


def parse_credentials(blob: str | None) -> dict:
    try:
        data = json.loads(blob or "")
    except ValueError:
        raise SwitchError("stored credentials are not valid JSON") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not (isinstance(oauth, dict) and oauth.get("accessToken") and oauth.get("refreshToken")):
        raise SwitchError("not a Claude subscription login (API-key logins are unsupported)")
    return oauth


# ------------------------------------------------------------------ claude.json


def read_claude_json() -> dict:
    try:
        data = json.loads(claude_json_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SwitchError(f"cannot read ~/.claude.json: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise SwitchError("~/.claude.json is not a JSON object")
    return data


def live_identity() -> tuple[dict | None, str | None]:
    """The oauthAccount block Claude Code last wrote, and its accountUuid."""
    oa = read_claude_json().get("oauthAccount")
    if not isinstance(oa, dict):
        return None, None
    uuid = oa.get("accountUuid")
    return oa, uuid if isinstance(uuid, str) and uuid else None


def set_claude_json_account(oauth_account: dict) -> None:
    """Replace only `oauthAccount`, keeping every other key, under Claude's lock."""
    path = claude_json_path().resolve()
    lock = path.with_name(path.name + ".lock")
    with dir_lock(lock, CONFIG_LOCK_STALE_S):
        cfg = read_claude_json()  # re-read inside the lock: Claude writes this often
        cfg["oauthAccount"] = oauth_account
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        atomic_write(path, json.dumps(cfg, indent=2, ensure_ascii=False), mode)


# ------------------------------------------------------------------------ state


def load_state() -> dict:
    try:
        data = json.loads((state_dir() / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise SwitchError(f"state.json unreadable: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise SwitchError("state.json is not a JSON object")
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = {}
    return data


def save_state(state: dict) -> None:
    ensure_state_dir()
    atomic_write(state_dir() / "state.json", json.dumps(state, indent=2))


def state_lock(timeout_s: float = 5.0):
    return file_lock(state_dir() / "state.lock", timeout_s)


def label_for_uuid(state: dict, uuid: str | None) -> str | None:
    if not uuid:
        return None
    for label, acct in state["accounts"].items():
        if acct.get("uuid") == uuid:
            return label
    return None


# ----------------------------------------------------------------------- policy


def effective(usage: dict | None, now: float) -> dict[str, float]:
    """Percent used per window; a window whose reset time has passed counts as 0."""
    out: dict[str, float] = {}
    for key in WINDOWS:
        w = (usage or {}).get(key)
        if not isinstance(w, dict) or not isinstance(w.get("pct"), (int, float)):
            continue
        resets = w.get("resets_at")
        rolled = isinstance(resets, (int, float)) and resets <= now
        out[key] = 0.0 if rolled else float(w["pct"])
    return out


def over_line(pcts: dict[str, float]) -> bool:
    return pcts.get("five_hour", 0.0) >= threshold() or pcts.get("seven_day", 0.0) >= weekly_threshold()


def at_limit(state: dict, active: str, now: float) -> bool:
    pcts = effective(state["accounts"][active].get("usage"), now)
    return max(pcts.values(), default=0.0) >= 100.0


def evaluate(state: dict, active: str, now: float) -> tuple[str, str | None]:
    """('ok', None) | ('switch', target) | ('stuck', None) when nothing has room |
    ('manual', None) when the active account is over its line but is manual-only."""
    mine = state["accounts"][active]
    if not over_line(effective(mine.get("usage"), now)):
        return "ok", None
    if mine.get("manual"):
        return "manual", None  # the user moves off a manual account themselves
    best: tuple[float, str] | None = None
    for label, acct in state["accounts"].items():
        if label == active or acct.get("manual"):
            continue
        pcts = effective(acct.get("usage"), now)
        if over_line(pcts):
            continue
        # A never-seen account may be fine or may be spent: usable, but after
        # any account known to have real room.
        score = max(pcts.values()) if pcts else UNKNOWN_SCORE
        if best is None or score < best[0]:
            best = (score, label)
    return ("switch", best[1]) if best else ("stuck", None)


def record_usage(acct: dict, rate_limits: dict, now: float) -> None:
    """Keep the highest reading seen per window. Each Claude Code session reports
    the usage from its own last response, so an idle session reports an old, lower
    number; within one window usage never falls, so a lower reading is stale."""
    old = acct.get("usage") or {}
    usage: dict = {"ts": now}
    for key in WINDOWS:
        w = rate_limits.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
            pct = float(w["used_percentage"])
            resets = w.get("resets_at")
            resets = float(resets) if isinstance(resets, (int, float)) else None
            prev = old.get(key)
            if (
                isinstance(prev, dict) and isinstance(prev.get("pct"), (int, float))
                and isinstance(prev.get("resets_at"), (int, float)) and resets is not None
                and abs(prev["resets_at"] - resets) <= SAME_WINDOW_S
            ):
                pct = max(pct, float(prev["pct"]))
            usage[key] = {"pct": pct, "resets_at": resets}
    if len(usage) > 1:
        acct["usage"] = usage


# ------------------------------------------------------------------- operations


def launch_detached(args: list[str]) -> None:
    """Run a subcommand of ourselves outliving the statusline call, which Claude
    Code cancels when a newer update arrives."""
    subprocess.Popen(
        [sys.executable, "-I", os.path.abspath(__file__), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def do_switch(target: str, auto: bool, force: bool = False) -> str:
    check_environment()
    with file_lock(state_dir() / "switch.lock", 15.0):
        now = time.time()
        with state_lock():
            state = load_state()
            if target not in state["accounts"]:
                raise SwitchError(f"unknown account {target!r}")
            if auto:
                active = state.get("active")
                if state["accounts"][target].get("manual"):
                    return f"{target} is manual-only; not switching"
                hard = active in state["accounts"] and at_limit(state, active, now)
                if now < state.get("cooldownUntil", 0) and not hard:
                    return "cooldown active; not switching"
                if active in state["accounts"] and evaluate(state, active, now)[1] != target:
                    return "no longer needed; not switching"

        live_oa, live_uuid = live_identity()
        outgoing = label_for_uuid(state, live_uuid)
        if outgoing == target:
            return f"{target} is already active"
        if outgoing is None and not force:
            raise SwitchError(
                "the logged-in Claude account is not managed by autoswap; "
                "switching would discard that login. Run `autoswap add <label>` first "
                "(or --force to discard it)"
            )
        target_creds = KEYCHAIN.get(SLOT_SERVICE, target)
        parse_credentials(target_creds)
        target_oa = state["accounts"][target].get("oauthAccount")
        if not isinstance(target_oa, dict):
            raise SwitchError(f"no identity stored for {target!r}; re-run `autoswap add {target}`")

        live_acct = live_account_name()
        with dir_lock(claude_home() / ".oauth_refresh.lock", CRED_LOCK_STALE_S), \
                dir_lock(claude_home().parent / (claude_home().name + ".lock"), CRED_LOCK_STALE_S):
            # Re-read under Claude's locks: a refresh may have just rotated the token,
            # and only this copy still holds a valid refresh token for the outgoing slot.
            live_creds = read_live_credentials()
            if outgoing is not None:
                try:
                    parse_credentials(live_creds)
                    write_verified(SLOT_SERVICE, outgoing, account_part(live_creds))
                except SwitchError as e:
                    log(f"write-back for {outgoing} skipped: {e}")
            try:
                write_verified(LIVE_SERVICE, live_acct, compose_live(target_creds, live_creds), allow_argv=True)
                set_claude_json_account(target_oa)
            except Exception:
                if live_creds is not None:
                    try:
                        KEYCHAIN.set(LIVE_SERVICE, live_acct, live_creds, allow_argv=True)
                    except SwitchError:
                        log("ROLLBACK FAILED: live login may need `claude /login`")
                raise

        with state_lock():
            state = load_state()
            if outgoing in state["accounts"] and isinstance(live_oa, dict):
                state["accounts"][outgoing]["oauthAccount"] = live_oa
            state.update(
                active=target, cooldownUntil=now + COOLDOWN_S,
                settleUntil=now + SETTLE_S, pendingAt=0, lastSyncAt=now,
                lastSwitchAt=now, polledAt=0,  # the new account's real usage is unknown: poll soon
            )
            save_state(state)
        msg = f"switched {outgoing or '?'} -> {target}" + (" (auto)" if auto else "")
        log(msg)
        return msg


def do_sync() -> str:
    """Copy Claude's (possibly refreshed) login for the active account into its slot."""
    check_environment()
    with file_lock(state_dir() / "switch.lock", 15.0):
        with state_lock():
            state = load_state()
        oa, uuid = live_identity()
        label = label_for_uuid(state, uuid)
        if label is None:
            return "active login is not managed; nothing to sync"
        creds = read_live_credentials()
        parse_credentials(creds)
        if live_identity()[1] != uuid:  # the login changed under us; don't mislabel it
            return "login changed during sync; skipped"
        if KEYCHAIN.get(SLOT_SERVICE, label) != account_part(creds):
            write_verified(SLOT_SERVICE, label, account_part(creds))
            log(f"synced refreshed login into {label}")
        with state_lock():
            state = load_state()
            state["accounts"][label]["oauthAccount"] = oa
            state["lastSyncAt"] = time.time()
            save_state(state)
        return f"synced {label}"


def _write_config(**updates: object) -> None:
    cfg = _read_config()
    cfg.update(updates)
    ensure_state_dir()
    atomic_write(state_dir() / "config.json", json.dumps(cfg, indent=2))


def find_claude() -> str | None:
    """The real path of the claude executable (a remembered one first: the shell
    PATH entries a version manager creates can disappear with the shell)."""
    home = Path.home()
    for cand in (_read_config().get("claudePath"), shutil.which("claude"), home / ".local/bin/claude",
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.realpath(cand)
    return None


def run_usage_command() -> str:
    """Run Claude Code's own `/usage` headlessly: the real client asks for the real
    number, so autoswap sends nothing to Anthropic itself. Hooks are off so a poll does
    not register as a session in whatever the user's hooks report to."""
    exe = find_claude()
    if exe is None:
        raise SwitchError("cannot find the claude executable")
    workdir = ensure_state_dir() / "poll"
    workdir.mkdir(mode=0o700, exist_ok=True)
    try:
        r = subprocess.run(
            [exe, "-p", "/usage", "--no-session-persistence", "--settings", '{"disableAllHooks": true}'],
            capture_output=True, text=True, timeout=POLL_TIMEOUT_S, cwd=workdir, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise SwitchError(f"claude /usage did not run: {type(e).__name__}") from e
    if r.returncode != 0:
        raise SwitchError(f"claude /usage failed (rc={r.returncode})")
    if _read_config().get("claudePath") != exe:
        _write_config(claudePath=exe)
    return r.stdout


_RESET_RE = re.compile(
    r"^(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\s+at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.I)
_USAGE_RE = re.compile(r"^Current (session|week)(?: \(([^)]*)\))?:\s*([\d.]+)% used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M)


def parse_reset(text: str, now: float) -> float | None:
    """'Oct 27 at 10:10pm (Asia/Calcutta)' or '10pm (Asia/Calcutta)' -> epoch seconds."""
    m = _RESET_RE.match(text.strip())
    if not m:
        return None
    mon, day, hour, minute, ampm, tzname = m.groups()
    try:
        tz = ZoneInfo(tzname.strip())
        h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
        mi = int(minute or 0)
        local_now = datetime.fromtimestamp(now, tz)
        if mon:
            month = datetime.strptime(mon.title(), "%b").month
            dt = datetime(local_now.year, month, int(day), h, mi, tzinfo=tz)
            if dt.timestamp() < now - 30 * 86400:  # a date far in the past is next year's
                dt = dt.replace(year=dt.year + 1)
        else:
            dt = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if dt.timestamp() <= now:
                dt += timedelta(days=1)
    except (ValueError, KeyError, OSError):
        return None
    return dt.timestamp()


def parse_usage(text: str, now: float) -> dict[str, dict]:
    """The 5h and all-models weekly windows from `/usage` output. Per-model weekly
    lines ('Current week (Fable)') are not account-wide and are ignored. An
    unrecognised format returns {} so the caller falls back to the statusline."""
    out: dict[str, dict] = {}
    for kind, label, pct, resets in _USAGE_RE.findall(text):
        if kind == "session":
            key = "five_hour"
        elif not label or label.strip().lower() in ("all models", "all"):
            key = "seven_day"
        else:
            continue
        out[key] = {"pct": float(pct), "resets_at": parse_reset(resets, now) if resets else None}
    return out


def poll_due(state: dict, label: str, now: float) -> bool:
    """Poll often when near a line, rarely when far: the interval is the headroom
    to the nearest line times POLL_S_PER_PCT, clamped."""
    if now - state.get("pollPendingAt", 0) <= POLL_PENDING_S:
        return False
    pcts = effective(state["accounts"][label].get("usage"), now)
    gaps = []
    if "five_hour" in pcts:
        gaps.append(threshold() - pcts["five_hour"])
    if "seven_day" in pcts:
        gaps.append(weekly_threshold() - pcts["seven_day"])
    interval = min(POLL_MAX_S, max(POLL_MIN_S, min(gaps, default=0.0) * POLL_S_PER_PCT))
    return now - state.get("polledAt", 0) >= interval


def switch_target(state: dict, label: str, now: float) -> str | None:
    """Where to move now, or None. Hard at the limit overrides the cooldown:
    waiting it out on a spare that turned out to be exhausted strands the session."""
    verdict, target = evaluate(state, label, now)
    if verdict != "switch":
        return None
    return target if now >= state.get("cooldownUntil", 0) or at_limit(state, label, now) else None


def do_poll() -> str:
    """Ask Claude Code for the active account's real usage, record it, and switch if due."""
    check_environment()
    try:
        lock = file_lock(state_dir() / "poll.lock", 0.1)
        lock.__enter__()
    except LockTimeout:
        return "a poll is already running"
    try:
        _, uuid_before = live_identity()
        with state_lock():
            state = load_state()
            label = label_for_uuid(state, uuid_before)
            switched_before = state.get("lastSwitchAt", 0)
        if label is None:
            return "active login is not managed; nothing to poll"
        windows = parse_usage(run_usage_command(), time.time())
        if "five_hour" not in windows:
            raise SwitchError("could not read the 5h figure from `claude /usage` (output format changed?)")
        now = time.time()
        _, uuid_after = live_identity()
        with state_lock():
            state = load_state()
            if uuid_after != uuid_before or state.get("lastSwitchAt", 0) != switched_before:
                return "login changed during the poll; reading discarded"
            # Authoritative, unlike a statusline reading: replace, don't max-merge.
            state["accounts"][label]["usage"] = {"ts": now, **windows}
            state.update(active=label, polledAt=now)
            target = switch_target(state, label, now)
            save_state(state)
        summary = f"{label}: " + "  ".join(f"{SHORT[k]} {v['pct']:g}%" for k, v in windows.items())
        if target and os.environ.get("AUTOSWAP_DRY_RUN"):
            log(f"dry run: would switch {label} -> {target}")
        elif target:
            summary += f" | {do_switch(target, auto=True)}"
        return summary
    finally:
        lock.__exit__(None, None, None)


def status_line(payload: dict) -> str:
    """Record usage for the active account and, when it nears its limit, launch a
    detached switch. Called on every Claude Code update; must stay fast."""
    check_environment()
    now = time.time()
    _, uuid = live_identity()
    rate_limits = payload.get("rate_limits")
    spawn: list[str] | None = None
    with state_lock(timeout_s=1.0):
        state = load_state()
        label = label_for_uuid(state, uuid)
        if label is None:
            return "autoswap: login not added"
        acct = state["accounts"][label]
        state["active"] = label
        if isinstance(rate_limits, dict) and now >= state.get("settleUntil", 0):
            record_usage(acct, rate_limits, now)
        verdict, target = evaluate(state, label, now)
        # Hard at the limit overrides the cooldown: waiting out 10 minutes on a
        # spare that turned out to be exhausted would leave the session stuck.
        cooled = now >= state.get("cooldownUntil", 0) or at_limit(state, label, now)
        idle = cooled and now - state.get("pendingAt", 0) > PENDING_S
        if verdict == "switch" and idle:
            if os.environ.get("AUTOSWAP_DRY_RUN"):
                log(f"dry run: would switch {label} -> {target}")
                state["pendingAt"] = now  # throttle the log line like a real launch
            else:
                spawn = ["switch", target, "--auto"]
                state["pendingAt"] = now
        elif poll_due(state, label, now):
            spawn = ["poll"]
            state["pollPendingAt"] = now
        elif now - state.get("lastSyncAt", 0) > SYNC_EVERY_S and now - state.get("pendingAt", 0) > PENDING_S:
            spawn = ["sync"]
            state["pendingAt"] = now
        save_state(state)
        pcts = effective(acct.get("usage"), now)
    if spawn:
        launch_detached(spawn)
    parts = [label]
    if "five_hour" in pcts:
        parts.append(f"5h {pcts['five_hour']:.0f}%")
    if "seven_day" in pcts:
        parts.append(f"7d {pcts['seven_day']:.0f}%")
    if acct.get("manual"):
        parts.append("manual")
    out = " · ".join(parts)
    if verdict == "switch":
        out += f" → {target}"
    elif verdict == "stuck":
        out += " ⚠ no spare account has room"
    elif verdict == "manual":
        out += " ⚠ near limit; switch manually"
    return out


# --------------------------------------------------------------------- commands


def _label(value: str) -> str:
    if not LABEL_RE.match(value):
        raise argparse.ArgumentTypeError("label must be 1-32 chars of letters, digits, _ or -")
    return value


def cmd_add(args: argparse.Namespace) -> int:
    check_environment()
    creds = read_live_credentials()
    if creds is None:
        raise SwitchError("Claude Code is not logged in (no stored login found)")
    parse_credentials(creds)
    oa, uuid = live_identity()
    if oa is None or uuid is None:
        raise SwitchError("~/.claude.json has no oauthAccount; run `claude` once and log in")
    with file_lock(state_dir() / "switch.lock", 15.0), state_lock():
        state = load_state()
        for other, acct in state["accounts"].items():
            if acct.get("uuid") == uuid and other != args.label:
                raise SwitchError(f"this login is already stored as {other!r}")
        prev = state["accounts"].get(args.label)
        if prev and prev.get("uuid") != uuid and not args.force:
            raise SwitchError(f"{args.label!r} holds a different account; pick another label or --force")
        write_verified(SLOT_SERVICE, args.label, account_part(creds))
        state["accounts"][args.label] = {
            "uuid": uuid, "email": oa.get("emailAddress"), "oauthAccount": oa,
            "usage": prev.get("usage") if prev else None,
            "manual": bool(args.manual or (prev and prev.get("manual"))),
        }
        state["active"] = args.label
        state["lastSyncAt"] = time.time()  # the slot was just captured from the live login
        save_state(state)
    print(f"stored {oa.get('emailAddress') or uuid} as {args.label!r}")
    return 0


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def cmd_list(_: argparse.Namespace) -> int:
    now = time.time()
    with state_lock():
        state = load_state()
    if not state["accounts"]:
        print("no accounts; log in with `claude`, then `autoswap add <label>`")
        return 0
    _, uuid = live_identity()
    active = label_for_uuid(state, uuid)
    for label, acct in state["accounts"].items():
        p = effective(acct.get("usage"), now)
        use = "  ".join(f"{SHORT[k]} {v:.0f}%" for k, v in p.items())
        ts = (acct.get("usage") or {}).get("ts")
        if use and isinstance(ts, (int, float)):
            use += f"  ({_ago(now - ts)})"
        mark = "*" if label == active else " "
        tag = "  [manual-only]" if acct.get("manual") else ""
        print(f"{mark} {label:<12} {acct.get('email') or '?':<32} {use or 'usage unknown'}{tag}")
    print(f"switch lines: 5h {threshold():.0f}%  7d {weekly_threshold():.0f}%")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    print(do_switch(args.label, auto=args.auto, force=args.force))
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    print(do_sync())
    return 0


def cmd_poll(_: argparse.Namespace) -> int:
    print(do_poll())
    return 0


def cmd_manual(args: argparse.Namespace) -> int:
    with state_lock():
        state = load_state()
        if args.label not in state["accounts"]:
            raise SwitchError(f"unknown account {args.label!r}")
        state["accounts"][args.label]["manual"] = args.mode == "on"
        save_state(state)
    if args.mode == "on":
        print(f"{args.label!r} is manual-only: never auto-switched to or away from")
    else:
        print(f"{args.label!r} is back in the automatic rotation")
    return 0


def cmd_threshold(args: argparse.Namespace) -> int:
    cfg = _read_config()
    for key, value in (("threshold", args.five_hour), ("weeklyThreshold", args.weekly)):
        if value is not None:
            if not 50 <= value <= 99:
                raise SwitchError("a switch line must be between 50 and 99")
            cfg[key] = value
    if args.five_hour is not None or args.weekly is not None:
        ensure_state_dir()
        atomic_write(state_dir() / "config.json", json.dumps(cfg, indent=2))
    print(f"switch lines: 5h {threshold():g}%  7d {weekly_threshold():g}%")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    with file_lock(state_dir() / "switch.lock", 15.0), state_lock():
        state = load_state()
        if args.label not in state["accounts"]:
            raise SwitchError(f"unknown account {args.label!r}")
        KEYCHAIN.delete(SLOT_SERVICE, args.label)
        del state["accounts"][args.label]
        save_state(state)
    print(f"removed {args.label!r} (Claude Code's own login is untouched)")
    return 0


def cmd_statusline(_: argparse.Namespace) -> int:
    # Never raise or exit non-zero: a broken statusline must not disturb Claude Code.
    try:
        payload = json.load(sys.stdin)
        print(status_line(payload if isinstance(payload, dict) else {}))
    except Exception as e:  # noqa: BLE001
        log(f"statusline: {type(e).__name__}: {e}")
        print("autoswap: error (see ~/.autoswap/autoswap.log)")
    return 0


def cmd_snippet(_: argparse.Namespace) -> int:
    cmd = f"{sys.executable} -I {os.path.abspath(__file__)} statusline"
    print("Add this to ~/.claude/settings.json:")
    print(json.dumps({"statusLine": {"type": "command", "command": cmd}}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoswap", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="store the account Claude Code is logged into now")
    a.add_argument("label", type=_label)
    a.add_argument("--force", action="store_true", help="overwrite a label holding another account")
    a.add_argument("--manual", action="store_true", help="never auto-switch to or away from this account")
    a.set_defaults(fn=cmd_add)
    t = sub.add_parser("threshold", help="show or set the switch lines (percent)")
    t.add_argument("five_hour", nargs="?", type=float, help="5h line, 50-99")
    t.add_argument("--weekly", type=float, help="7d line, 50-99")
    t.set_defaults(fn=cmd_threshold)
    m = sub.add_parser("manual", help="mark an account manual-only (or put it back in rotation)")
    m.add_argument("label", type=_label)
    m.add_argument("mode", nargs="?", choices=("on", "off"), default="on")
    m.set_defaults(fn=cmd_manual)
    sub.add_parser("list", help="accounts and last-seen usage").set_defaults(fn=cmd_list)
    s = sub.add_parser("switch", help="switch Claude Code to a stored account now")
    s.add_argument("label", type=_label)
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--force", action="store_true", help="discard an unmanaged live login")
    s.set_defaults(fn=cmd_switch)
    r = sub.add_parser("remove", help="forget a stored account")
    r.add_argument("label", type=_label)
    r.set_defaults(fn=cmd_remove)
    sub.add_parser("statusline", help="Claude Code statusLine command").set_defaults(fn=cmd_statusline)
    sub.add_parser("sync", help=argparse.SUPPRESS).set_defaults(fn=cmd_sync)
    sub.add_parser("poll", help="ask Claude Code for the active account's real usage now").set_defaults(fn=cmd_poll)
    sub.add_parser("snippet", help="print the settings.json statusLine entry").set_defaults(fn=cmd_snippet)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except SwitchError as e:
        print(f"autoswap: {e}", file=sys.stderr)
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
