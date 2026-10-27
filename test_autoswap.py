"""Tests for autoswap. They run against an in-memory keychain and a temp HOME:
nothing here can touch the real Keychain, ~/.claude.json or the network."""

import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import autoswap as A  # noqa: E402

SECRET_MARKERS = ("ACCESS-", "REFRESH-")


class MemKeychain:
    def __init__(self):
        self.d = {}

    def get(self, service, account):
        return self.d.get((service, account))

    def set(self, service, account, secret, allow_argv=False):
        self.d[(service, account)] = secret

    def delete(self, service, account):
        self.d.pop((service, account), None)


def creds(tag, refresh_gen=1, **extra):
    """A Claude Code credential blob as it sits in the Keychain (compact JSON)."""
    data = {"claudeAiOauth": {
        "accessToken": f"ACCESS-{tag}-{refresh_gen}",
        "refreshToken": f"REFRESH-{tag}-{refresh_gen}",
        "expiresAt": 1,
    }}
    data.update(extra)
    return json.dumps(data, separators=(",", ":"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USER": "tester"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "AUTOSWAP_THRESHOLD",
                    "AUTOSWAP_WEEKLY_THRESHOLD", "AUTOSWAP_DRY_RUN"):
            os.environ.pop(var, None)
        (self.home / ".claude").mkdir()
        self.kc = MemKeychain()
        for target, new in (
            (A, ("KEYCHAIN", self.kc)),
            (A, ("launch_detached", lambda args: self.launched.append(args))),
            (A, ("LOCK_TIMEOUT_S", 0.3)),
        ):
            p = mock.patch.object(target, new[0], new[1])
            p.start()
            self.addCleanup(p.stop)
        self.launched = []
        self.cfg_path = self.home / ".claude.json"
        # Polling is exercised by PollTests; elsewhere it would add a launch to every statusline call.
        self._poll_patch = mock.patch.object(A, "poll_due", return_value=False)
        self._poll_patch.start()
        self.addCleanup(lambda: self._poll_patch and self._poll_patch.stop())

    def enable_polling(self):
        self._poll_patch.stop()
        self._poll_patch = None

    # -- helpers
    def login(self, tag, uuid, gen=1):
        """Simulate `claude /login` as account `tag`."""
        self.kc.d[(A.LIVE_SERVICE, "tester")] = creds(tag, gen)
        cfg = json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {
            "numStartups": 7, "projects": {"/x": {"allowedTools": ["a"]}}}
        cfg["oauthAccount"] = {"accountUuid": uuid, "emailAddress": f"{tag}@example.com"}
        self.cfg_path.write_text(json.dumps(cfg, indent=2))
        os.chmod(self.cfg_path, 0o600)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = A.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def two_accounts(self):
        self.login("A", "uuid-a")
        self.cli("add", "a")
        self.login("B", "uuid-b")
        self.cli("add", "b")  # live login is now B, managed as "b"

    def status(self, five=None, seven=None):
        rl = {}
        if five is not None:
            rl["five_hour"] = {"used_percentage": five, "resets_at": time.time() + 3600}
        if seven is not None:
            rl["seven_day"] = {"used_percentage": seven, "resets_at": time.time() + 86400}
        return A.status_line({"rate_limits": rl})

    def state(self):
        return json.loads((self.home / ".autoswap" / "state.json").read_text())

    def edit_state(self, fn):
        s = self.state()
        fn(s)
        (self.home / ".autoswap" / "state.json").write_text(json.dumps(s))

    def live_uuid(self):
        return json.loads(self.cfg_path.read_text())["oauthAccount"]["accountUuid"]


class AddTests(Base):
    def test_add_stores_secret_only_in_keychain(self):
        self.two_accounts()
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "a")], creds("A"))
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "b")], creds("B"))
        text = (self.home / ".autoswap" / "state.json").read_text()
        self.assertFalse(any(m in text for m in SECRET_MARKERS))
        self.assertEqual(stat.S_IMODE((self.home / ".autoswap").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / ".autoswap" / "state.json").stat().st_mode), 0o600)

    def test_same_login_under_two_labels_rejected(self):
        self.two_accounts()
        rc, _, err = self.cli("add", "again")
        self.assertEqual(rc, 1)
        self.assertIn("already stored", err)

    def test_label_holding_other_account_needs_force(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.assertEqual(self.cli("add", "a")[0], 1)
        self.assertEqual(self.cli("add", "a", "--force")[0], 0)

    def test_api_key_login_rejected(self):
        self.login("A", "uuid-a")
        self.kc.d[(A.LIVE_SERVICE, "tester")] = json.dumps({"apiKey": "sk-ant-x"})
        self.assertEqual(self.cli("add", "a")[0], 1)

    def test_bad_label_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                A.main(["add", "../evil"])


class PolicyTests(Base):
    def test_below_line_does_nothing(self):
        self.two_accounts()
        self.status(five=50, seven=10)
        self.assertEqual(self.launched, [])

    def test_over_line_launches_switch_once(self):
        self.two_accounts()
        line = self.status(five=95)
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])
        self.assertIn("→ a", line)
        self.status(five=96)  # pendingAt throttles a second launch
        self.assertEqual(len(self.launched), 1)

    def test_weekly_line_is_separate(self):
        self.two_accounts()
        self.status(five=10, seven=92)  # under the 95 weekly line
        self.assertEqual(self.launched, [])
        os.environ["AUTOSWAP_WEEKLY_THRESHOLD"] = "90"
        self.status(five=10, seven=92)
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])

    def test_no_viable_target_holds_still(self):
        self.two_accounts()
        self.edit_state(lambda s: s["accounts"]["a"].update(
            usage={"five_hour": {"pct": 99.0, "resets_at": time.time() + 600}}))
        line = self.status(five=95)
        self.assertEqual(self.launched, [])
        self.assertIn("no spare", line)

    def test_reset_window_makes_candidate_viable(self):
        self.two_accounts()
        self.edit_state(lambda s: s["accounts"]["a"].update(
            usage={"five_hour": {"pct": 99.0, "resets_at": time.time() - 5}}))
        self.status(five=95)
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])

    def test_picks_candidate_with_most_room(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.cli("add", "c")
        self.login("B", "uuid-b")  # back on b
        soon = time.time() + 600
        self.edit_state(lambda s: (
            s["accounts"]["a"].update(usage={"five_hour": {"pct": 70.0, "resets_at": soon}}),
            s["accounts"]["c"].update(usage={"five_hour": {"pct": 20.0, "resets_at": soon}})))
        self.status(five=95)
        self.assertEqual(self.launched, [["switch", "c", "--auto"]])

    def test_known_room_beats_a_never_seen_account(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.cli("add", "c")  # c has no reading yet
        self.login("B", "uuid-b")
        soon = time.time() + 600
        self.edit_state(lambda s: s["accounts"]["a"].update(
            usage={"five_hour": {"pct": 5.0, "resets_at": soon}}))
        self.status(five=95)
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])
        # ...but a never-seen account still beats one that is nearly full
        self.launched.clear()
        self.edit_state(lambda s: s["accounts"]["a"].update(
            usage={"five_hour": {"pct": 60.0, "resets_at": soon}}, ) or s.update(pendingAt=0))
        self.status(five=95)
        self.assertEqual(self.launched, [["switch", "c", "--auto"]])

    def test_threshold_command_persists_and_every_surface_honours_it(self):
        self.two_accounts()
        self.assertIn("5h 90%", self.cli("threshold")[1])
        self.assertIn("5h 85%", self.cli("threshold", "85")[1])
        self.assertIn("switch lines: 5h 85%", self.cli("list")[1])
        self.status(five=86)  # over 85, under the old 90
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])
        self.assertEqual(stat.S_IMODE((self.home / ".autoswap" / "config.json").stat().st_mode), 0o600)

    def test_env_overrides_the_config_file_and_bad_values_are_rejected(self):
        self.cli("threshold", "85", "--weekly", "92")
        self.assertEqual((A.threshold(), A.weekly_threshold()), (85.0, 92.0))
        os.environ["AUTOSWAP_THRESHOLD"] = "70"
        self.assertEqual(A.threshold(), 70.0)
        os.environ.pop("AUTOSWAP_THRESHOLD")
        self.assertEqual(self.cli("threshold", "120")[0], 1)
        self.assertEqual(self.cli("threshold", "10")[0], 1)
        (self.home / ".autoswap" / "config.json").write_text("{corrupt")
        self.assertEqual(A.threshold(), 90.0)  # unreadable config falls back to the default

    def test_a_stale_lower_reading_from_an_idle_session_does_not_lower_the_record(self):
        self.two_accounts()
        self.status(five=89)
        self.status(five=78)  # another session that hasn't heard back since earlier
        self.assertEqual(self.state()["accounts"]["b"]["usage"]["five_hour"]["pct"], 89.0)
        self.status(five=91)
        self.assertEqual(self.state()["accounts"]["b"]["usage"]["five_hour"]["pct"], 91.0)

    def test_a_new_window_replaces_the_old_reading(self):
        acct = {"usage": {"five_hour": {"pct": 95.0, "resets_at": 1000.0}}}
        A.record_usage(acct, {"five_hour": {"used_percentage": 3, "resets_at": 1000.0 + 5 * 3600}}, 0)
        self.assertEqual(acct["usage"]["five_hour"]["pct"], 3.0)
        A.record_usage(acct, {"five_hour": {"used_percentage": 2, "resets_at": 1000.0 + 5 * 3600 + 2}}, 0)
        self.assertEqual(acct["usage"]["five_hour"]["pct"], 3.0)  # same window again: keep the max

    def test_stale_low_reading_cannot_hide_an_account_that_is_over_the_line(self):
        self.two_accounts()
        self.status(five=92)
        self.launched.clear()
        self.edit_state(lambda s: s.update(pendingAt=0, cooldownUntil=0))
        self.status(five=70)  # stale session fires the statusline
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])

    def test_list_shows_how_old_each_reading_is(self):
        self.two_accounts()
        now = time.time()
        self.edit_state(lambda s: s["accounts"]["a"].update(usage={
            "ts": now - 7200, "five_hour": {"pct": 40.0, "resets_at": now + 600}}))
        out = self.cli("list")[1]
        self.assertIn("5h 40%  (2h ago)", out)
        self.assertIn("usage unknown", out)  # b has no reading
        self.assertEqual([A._ago(s) for s in (5, 600, 7200, 300000)],
                         ["just now", "10m ago", "2h ago", "3d ago"])

    def test_dry_run_never_launches(self):
        self.two_accounts()
        os.environ["AUTOSWAP_DRY_RUN"] = "1"
        self.status(five=95)
        self.assertEqual(self.launched, [])
        self.assertIn("dry run", (self.home / ".autoswap" / "autoswap.log").read_text())

    def test_unmanaged_login_is_ignored(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.assertIn("not added", self.status(five=99))
        self.assertEqual(self.launched, [])

    def test_first_run_syncs_when_overdue(self):
        self.two_accounts()
        self.edit_state(lambda s: s.update(lastSyncAt=0))
        self.status(five=10)
        self.assertEqual(self.launched, [["sync"]])


class SwitchTests(Base):
    def test_switch_swaps_login_and_identity_keeps_other_config(self):
        self.two_accounts()
        self.assertEqual(self.cli("switch", "a")[0], 0)
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("A"))
        self.assertEqual(self.live_uuid(), "uuid-a")
        cfg = json.loads(self.cfg_path.read_text())
        self.assertEqual(cfg["numStartups"], 7)
        self.assertEqual(cfg["projects"], {"/x": {"allowedTools": ["a"]}})
        self.assertEqual(stat.S_IMODE(self.cfg_path.stat().st_mode), 0o600)
        self.assertEqual(self.state()["active"], "a")
        self.assertFalse((self.home / ".claude.lock").exists())
        self.assertFalse((self.home / ".claude" / ".oauth_refresh.lock").exists())
        self.assertFalse((self.home / ".claude.json.lock").exists())

    def test_switch_saves_claudes_refreshed_login_for_the_outgoing_slot(self):
        self.two_accounts()
        self.kc.d[(A.LIVE_SERVICE, "tester")] = creds("B", refresh_gen=2)  # Claude rotated it
        self.cli("switch", "a")
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "b")], creds("B", refresh_gen=2))

    def test_switch_back_and_forth_round_trips(self):
        self.two_accounts()
        self.cli("switch", "a")
        self.cli("switch", "b")
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("B"))
        self.assertEqual(self.live_uuid(), "uuid-b")

    def test_refuses_to_discard_unmanaged_login(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        rc, _, err = self.cli("switch", "a")
        self.assertEqual(rc, 1)
        self.assertIn("not managed", err)
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("C"))

    def test_failure_after_keychain_write_rolls_back(self):
        self.two_accounts()
        with mock.patch.object(A, "set_claude_json_account", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                A.do_switch("a", auto=False)
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("B"))
        self.assertEqual(self.live_uuid(), "uuid-b")

    def test_corrupt_claude_json_aborts_without_touching_login(self):
        self.two_accounts()
        real = A.set_claude_json_account

        def corrupt_then_switch(oa):
            self.cfg_path.write_text("{not json")
            return real(oa)

        with mock.patch.object(A, "set_claude_json_account", corrupt_then_switch):
            with self.assertRaises(A.SwitchError):
                A.do_switch("a", auto=False)
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("B"))
        self.assertEqual(self.cfg_path.read_text(), "{not json")  # never overwritten

    def test_held_claude_lock_aborts_cleanly(self):
        self.two_accounts()
        (self.home / ".claude.lock").mkdir()
        rc, _, err = self.cli("switch", "a")
        self.assertEqual(rc, 1)
        self.assertIn("held", err)
        self.assertEqual(self.kc.d[(A.LIVE_SERVICE, "tester")], creds("B"))
        self.assertTrue((self.home / ".claude.lock").exists())  # not ours, not removed

    def test_stale_claude_lock_is_taken_over(self):
        self.two_accounts()
        lock = self.home / ".claude.lock"
        lock.mkdir()
        old = time.time() - 120
        os.utime(lock, (old, old))
        self.assertEqual(self.cli("switch", "a")[0], 0)
        self.assertFalse(lock.exists())

    def test_cooldown_blocks_auto_but_not_manual(self):
        self.two_accounts()
        self.cli("switch", "a")
        self.assertIn("cooldown", A.do_switch("b", auto=True))
        self.assertEqual(self.live_uuid(), "uuid-a")
        self.assertEqual(self.cli("switch", "b")[0], 0)

    def test_auto_switch_rechecks_it_is_still_needed(self):
        self.two_accounts()
        self.edit_state(lambda s: s["accounts"]["b"].update(
            usage={"five_hour": {"pct": 10.0, "resets_at": time.time() + 600}}))
        self.assertIn("no longer needed", A.do_switch("a", auto=True))
        self.assertEqual(self.live_uuid(), "uuid-b")

    def test_readings_right_after_a_switch_are_not_attributed(self):
        self.two_accounts()
        self.cli("switch", "a")
        self.status(five=99)  # stale: describes the account we just left
        self.assertIsNone(self.state()["accounts"]["a"]["usage"])

    def test_custom_config_dir_is_refused(self):
        self.two_accounts()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home / "elsewhere")
        rc, _, err = self.cli("switch", "a")
        self.assertEqual(rc, 1)
        self.assertIn("CLAUDE_CONFIG_DIR", err)


class ManualAccountTests(Base):
    """Five accounts rotate automatically; `f` is manual-only."""

    AUTO = ("a", "b", "c", "d", "e")

    def six_accounts(self):
        for tag in "abcde":
            self.login(tag.upper(), f"uuid-{tag}")
            self.cli("add", tag)
        self.login("F", "uuid-f")
        self.cli("add", "f", "--manual")  # ends logged in as f

    def seed(self, **pct_by_label):
        soon = time.time() + 600

        def apply(s):
            for label, pct in pct_by_label.items():
                s["accounts"][label]["usage"] = {"five_hour": {"pct": float(pct), "resets_at": soon}}
        self.edit_state(apply)

    def test_manual_account_is_never_an_auto_target(self):
        self.six_accounts()
        self.login("A", "uuid-a")
        self.seed(b=90, c=40, d=95, e=99, f=0)  # f has the most room of all
        self.status(five=95)
        self.assertEqual(self.launched, [["switch", "c", "--auto"]])

    def test_manual_account_is_never_chosen_even_when_everything_else_is_full(self):
        self.six_accounts()
        self.login("A", "uuid-a")
        self.seed(b=95, c=95, d=95, e=95, f=0)
        line = self.status(five=95)
        self.assertEqual(self.launched, [])
        self.assertIn("no spare", line)

    def test_active_manual_account_is_left_alone(self):
        self.six_accounts()  # live login is f
        self.seed(a=0, b=0)
        line = self.status(five=99)
        self.assertEqual(self.launched, [])
        self.assertIn("switch manually", line)
        self.assertIn("manual", line)

    def test_auto_switch_refuses_a_manual_target(self):
        self.six_accounts()
        self.login("A", "uuid-a")
        self.assertIn("manual-only", A.do_switch("f", auto=True))
        self.assertEqual(self.live_uuid(), "uuid-a")

    def test_explicit_switch_works_to_and_from_a_manual_account(self):
        self.six_accounts()
        self.assertEqual(self.cli("switch", "a")[0], 0)
        self.assertEqual(self.cli("switch", "f")[0], 0)
        self.assertEqual(self.live_uuid(), "uuid-f")
        self.assertEqual(self.cli("switch", "b")[0], 0)
        self.assertEqual(self.live_uuid(), "uuid-b")

    def test_manual_command_toggles_and_list_shows_it(self):
        self.two_accounts()
        self.assertIn("manual-only", self.cli("manual", "a")[1])
        self.assertIn("[manual-only]", self.cli("list")[1])
        self.status(five=95)  # only spare is manual
        self.assertEqual(self.launched, [])
        self.cli("manual", "a", "off")
        self.assertNotIn("[manual-only]", self.cli("list")[1])
        self.status(five=96)
        self.assertEqual(self.launched, [["switch", "a", "--auto"]])

    def test_readding_keeps_the_manual_flag(self):
        self.six_accounts()
        self.cli("add", "f")
        self.assertTrue(self.state()["accounts"]["f"]["manual"])

    def test_manual_account_still_gets_its_refreshed_login_saved(self):
        self.six_accounts()
        self.kc.d[(A.LIVE_SERVICE, "tester")] = creds("F", refresh_gen=4)
        self.cli("sync")
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "f")], creds("F", refresh_gen=4))

    def test_hard_limit_bypasses_cooldown_but_a_mere_line_crossing_does_not(self):
        self.two_accounts()
        self.cli("switch", "a")  # starts the cooldown
        self.edit_state(lambda s: s.update(settleUntil=0))
        self.status(five=95)
        self.assertEqual(self.launched, [], "over the line, but cooldown holds")
        self.edit_state(lambda s: s["accounts"]["b"].update(usage=None))
        self.status(five=100)
        self.assertEqual(self.launched, [["switch", "b", "--auto"]])
        self.assertIn("switched", A.do_switch("b", auto=True))

    def test_rotation_visits_every_auto_account_then_holds_and_never_touches_manual(self):
        self.six_accounts()
        self.login("A", "uuid-a")
        visited = ["a"]
        for _ in range(10):
            self.edit_state(lambda s: s.update(cooldownUntil=0, settleUntil=0, pendingAt=0))
            self.launched.clear()
            self.status(five=95)
            if not self.launched:
                break
            _, target, _ = self.launched[0]
            A.do_switch(target, auto=True)
            visited.append(target)
        self.assertEqual(sorted(visited), list(self.AUTO))
        self.assertEqual(self.live_uuid(), f"uuid-{visited[-1]}")
        self.assertEqual(self.launched, [])  # all five over the line: holds, does not use f


class SharedKeysTests(Base):
    """Claude Code's login item also holds MCP logins (20 KB on a real machine).
    Those are machine-wide: they must never be stored per account or swapped."""

    def live(self):
        return json.loads(self.kc.d[(A.LIVE_SERVICE, "tester")])

    def login_with_mcp(self, tag, uuid, mcp, device=None):
        extra = {"mcpOAuth": mcp, "pluginSecrets": {"p": "PLUGINSECRET"}}
        if device:
            extra["trustedDeviceToken"] = device
        self.login(tag, uuid)
        self.kc.d[(A.LIVE_SERVICE, "tester")] = creds(tag, **extra)

    def test_slots_store_only_the_account_part(self):
        self.login_with_mcp("A", "uuid-a", {"notion": {"accessToken": "MCPTOKEN"}}, device="DEVICE-A")
        self.cli("add", "a")
        slot = json.loads(self.kc.d[(A.SLOT_SERVICE, "a")])
        self.assertEqual(sorted(slot), ["claudeAiOauth", "trustedDeviceToken"])
        self.assertNotIn("MCPTOKEN", self.kc.d[(A.SLOT_SERVICE, "a")])
        self.assertNotIn("PLUGINSECRET", self.kc.d[(A.SLOT_SERVICE, "a")])

    def test_switch_keeps_live_mcp_logins_and_swaps_account_fields(self):
        self.login_with_mcp("A", "uuid-a", {"notion": {"accessToken": "MCP-OLD"}}, device="DEVICE-A")
        self.cli("add", "a")
        self.login_with_mcp("B", "uuid-b", {"notion": {"accessToken": "MCP-OLD"}}, device="DEVICE-B")
        self.cli("add", "b")
        # Claude rotates an MCP token after both accounts were added; the live copy is the current one.
        self.login_with_mcp("B", "uuid-b", {"notion": {"accessToken": "MCP-NEW"}}, device="DEVICE-B")
        self.cli("switch", "a")
        live = self.live()
        self.assertEqual(live["mcpOAuth"], {"notion": {"accessToken": "MCP-NEW"}})
        self.assertEqual(live["pluginSecrets"], {"p": "PLUGINSECRET"})
        self.assertEqual(live["claudeAiOauth"]["refreshToken"], "REFRESH-A-1")
        self.assertEqual(live["trustedDeviceToken"], "DEVICE-A")  # account-bound: follows the slot

    def test_switch_away_saves_account_part_only(self):
        self.login_with_mcp("A", "uuid-a", {"n": "x"})
        self.cli("add", "a")
        self.login_with_mcp("B", "uuid-b", {"n": "x"})
        self.cli("add", "b")
        self.cli("switch", "a")
        self.assertEqual(json.loads(self.kc.d[(A.SLOT_SERVICE, "b")]).keys(), {"claudeAiOauth"})

    def test_big_login_item_round_trips_without_losing_mcp_logins(self):
        big = {f"server-{i}": {"accessToken": "t" * 350, "refreshToken": "r" * 150} for i in range(40)}
        self.login_with_mcp("A", "uuid-a", big)
        self.cli("add", "a")
        self.login_with_mcp("B", "uuid-b", big)
        self.cli("add", "b")
        self.assertGreater(len(self.kc.d[(A.LIVE_SERVICE, "tester")]), 15000)
        self.cli("switch", "a")
        self.assertEqual(self.live()["mcpOAuth"], big)

    def test_argv_is_used_only_for_an_explicitly_allowed_oversized_write(self):
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        huge = "x" * 20000
        with mock.patch.object(A.subprocess, "run", fake):
            kc = A.SecurityKeychain()
            with self.assertRaises(A.KeychainError):
                kc.set("svc", "acct", huge)  # default: refused
            fake.assert_not_called()
            kc.set("svc", "acct", huge, allow_argv=True)
            self.assertEqual(fake.call_args.args[0][:4], [A.SECURITY, "add-generic-password", "-U", "-a"])
            fake.reset_mock()
            kc.set("svc", "acct", "small", allow_argv=True)  # fits stdin: still stdin
            self.assertEqual(fake.call_args.args[0], [A.SECURITY, "-i"])

    def test_slot_writes_never_allow_argv(self):
        self.login("A", "uuid-a")
        calls = []
        real = self.kc.set
        self.kc.set = lambda s, a, v, allow_argv=False: (calls.append((s, allow_argv)), real(s, a, v))[1]
        self.cli("add", "a")
        self.assertEqual([c for c in calls if c[0] == A.SLOT_SERVICE], [(A.SLOT_SERVICE, False)])


REAL_USAGE_OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 16% used · resets Oct 27 at 10:10pm (Asia/Calcutta)
Current week (Fable): 0% used · resets Nov 2 at 8:30pm (Asia/Calcutta)

What's contributing to your limits usage?
Approximate · sessions on this machine only · shares overlap

Last 24h · 11734 requests · 6 sessions
  99%  Give simple subagents a cheaper model
"""


class PollTests(Base):
    """The statusline can't see usage from a workflow running in an idle chat, so
    autoswap asks Claude Code's own /usage (first-party) for the real number."""

    NOW = 1792000000.0  # fixed clock for the pure parsers

    def poll_with(self, text, **kw):
        with mock.patch.object(A, "run_usage_command", return_value=text):
            return A.do_poll(**kw)

    def test_parses_the_real_usage_output(self):
        w = A.parse_usage(REAL_USAGE_OUTPUT, self.NOW)
        self.assertEqual(list(w), ["five_hour"])  # the per-model weekly line is not account-wide
        self.assertEqual(w["five_hour"]["pct"], 16.0)
        self.assertIsNotNone(w["five_hour"]["resets_at"])

    def test_all_models_weekly_window_is_the_seven_day_figure(self):
        text = "Current session: 40% used · resets 3pm (UTC)\nCurrent week (all models): 72% used · resets Nov 2 at 8:30pm (UTC)\n"
        w = A.parse_usage(text, self.NOW)
        self.assertEqual((w["five_hour"]["pct"], w["seven_day"]["pct"]), (40.0, 72.0))

    def test_unrecognised_output_yields_nothing_rather_than_a_guess(self):
        for text in ("", "Unknown command: /usage", "Current session: lots used", "Current session: 5% used"):
            self.assertEqual(A.parse_usage(text, self.NOW).get("five_hour", {}).get("resets_at"), None)
        self.assertEqual(A.parse_usage("Unknown command: /usage", self.NOW), {})

    def test_reset_time_formats(self):
        # 2026-10-27 12:00 UTC; Asia/Calcutta is UTC+5:30
        now = datetime_epoch(2026, 10, 27, 12, 0)
        self.assertEqual(A.parse_reset("Oct 27 at 10:10pm (Asia/Calcutta)", now), datetime_epoch(2026, 10, 27, 16, 40))
        self.assertEqual(A.parse_reset("10pm (UTC)", now), datetime_epoch(2026, 10, 27, 22, 0))
        self.assertEqual(A.parse_reset("9am (UTC)", now), datetime_epoch(2026, 10, 28, 9, 0))  # already past: tomorrow
        self.assertEqual(A.parse_reset("Jan 2 at 12am (UTC)", datetime_epoch(2026, 12, 30, 0, 0)), datetime_epoch(2027, 1, 2, 0, 0))
        self.assertIsNone(A.parse_reset("soonish", now))
        self.assertIsNone(A.parse_reset("10pm (Not/AZone)", now))

    def test_poll_replaces_a_stale_statusline_reading_with_the_real_one(self):
        self.two_accounts()
        self.status(five=5)  # idle chat's stale view
        out = self.poll_with(REAL_USAGE_OUTPUT)
        self.assertIn("b: 5h 16%", out)
        self.assertEqual(self.state()["accounts"]["b"]["usage"]["five_hour"]["pct"], 16.0)
        self.assertGreater(self.state()["polledAt"], 0)

    def test_poll_switches_when_the_real_number_is_over_the_line(self):
        self.two_accounts()
        out = self.poll_with(REAL_USAGE_OUTPUT.replace("16%", "95%"))
        self.assertIn("switched b -> a (auto)", out)
        self.assertEqual(self.live_uuid(), "uuid-a")

    def test_poll_in_dry_run_logs_but_does_not_switch(self):
        self.two_accounts()
        os.environ["AUTOSWAP_DRY_RUN"] = "1"
        self.poll_with(REAL_USAGE_OUTPUT.replace("16%", "95%"))
        self.assertEqual(self.live_uuid(), "uuid-b")
        self.assertIn("dry run", (self.home / ".autoswap" / "autoswap.log").read_text())

    def test_poll_on_a_manual_account_records_usage_but_never_switches(self):
        self.two_accounts()
        self.cli("manual", "b")
        out = self.poll_with(REAL_USAGE_OUTPUT.replace("16%", "99%"))
        self.assertNotIn("switched", out)
        self.assertEqual(self.live_uuid(), "uuid-b")

    def test_poll_discards_a_reading_if_the_login_changed_underneath_it(self):
        self.two_accounts()

        def usage_then_someone_switches(*_):
            self.login("A", "uuid-a")  # a /login happened while claude was running
            return REAL_USAGE_OUTPUT

        with mock.patch.object(A, "run_usage_command", usage_then_someone_switches):
            self.assertIn("discarded", A.do_poll())
        self.assertIsNone(self.state()["accounts"]["b"]["usage"])
        self.assertIsNone(self.state()["accounts"]["a"]["usage"])

    def test_poll_that_cannot_be_read_changes_nothing(self):
        self.two_accounts()
        self.status(five=40)
        with self.assertRaises(A.SwitchError):
            self.poll_with("Unknown command: /usage")
        self.assertEqual(self.state()["accounts"]["b"]["usage"]["five_hour"]["pct"], 40.0)

    def test_only_one_poll_runs_at_a_time(self):
        self.two_accounts()
        with A.file_lock(self.home / ".autoswap" / "poll.lock", 1.0):
            self.assertIn("already running", self.poll_with(REAL_USAGE_OUTPUT))

    def test_unmanaged_login_is_not_polled(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.assertIn("not managed", self.poll_with(REAL_USAGE_OUTPUT))

    def test_statusline_launches_a_poll_when_due_and_only_once(self):
        self.two_accounts()
        self.enable_polling()
        self.status(five=10)
        self.assertEqual(self.launched, [["poll"]])
        self.status(five=10)
        self.assertEqual(self.launched, [["poll"]])  # pending: not relaunched

    def test_poll_cadence_tightens_near_the_line_and_relaxes_far_from_it(self):
        self.two_accounts()
        self.enable_polling()
        now = time.time()
        for pct, wait, expect in ((10, 130, False), (10, 700, True), (80, 130, True), (80, 60, False)):
            self.edit_state(lambda s, p=pct, w=wait: s.update(polledAt=now - w, pollPendingAt=0) or s["accounts"]["b"].update(
                usage={"ts": now, "five_hour": {"pct": float(p), "resets_at": now + 3600}}))
            self.assertEqual(A.poll_due(self.state(), "b", now), expect, (pct, wait))

    def test_a_switch_makes_the_next_poll_immediate(self):
        self.two_accounts()
        self.enable_polling()
        self.cli("switch", "a")
        self.assertEqual(self.state()["polledAt"], 0)

    def test_hooks_are_disabled_and_nothing_is_persisted_for_the_headless_run(self):
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout=REAL_USAGE_OUTPUT, stderr=""))
        exe = self.home / "claude"
        exe.write_text("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        with mock.patch.object(A.subprocess, "run", fake), mock.patch.object(A, "find_claude", return_value=str(exe)):
            self.assertEqual(A.run_usage_command(), REAL_USAGE_OUTPUT)
        argv = fake.call_args.args[0]
        self.assertEqual(argv[1:4], ["-p", "/usage", "--no-session-persistence"])
        self.assertIn('{"disableAllHooks": true}', argv)
        self.assertEqual(fake.call_args.kwargs["cwd"], self.home / ".autoswap" / "poll")

    def test_find_claude_prefers_the_remembered_path_and_survives_a_missing_one(self):
        real = self.home / "bin" / "claude"
        real.parent.mkdir()
        real.write_text("#!/bin/sh\n")
        os.chmod(real, 0o755)
        A._write_config(claudePath=str(real))
        self.assertEqual(A.find_claude(), os.path.realpath(real))
        A._write_config(claudePath=str(self.home / "gone"))
        with mock.patch.object(A.shutil, "which", return_value=None):
            self.assertNotEqual(A.find_claude(), str(self.home / "gone"))


def datetime_epoch(y, mo, d, h, mi):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class SyncTests(Base):
    def test_sync_copies_refreshed_login_into_active_slot(self):
        self.two_accounts()
        self.kc.d[(A.LIVE_SERVICE, "tester")] = creds("B", refresh_gen=3)
        self.assertEqual(self.cli("sync")[0], 0)
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "b")], creds("B", refresh_gen=3))

    def test_sync_never_labels_an_unmanaged_login(self):
        self.two_accounts()
        self.login("C", "uuid-c")
        self.assertIn("not managed", self.cli("sync")[1])
        self.assertEqual(self.kc.d[(A.SLOT_SERVICE, "b")], creds("B"))


class SafetyTests(Base):
    def test_secrets_never_reach_log_or_output(self):
        self.two_accounts()
        outputs = [self.cli("list"), self.cli("switch", "a"), self.cli("sync"), self.cli("switch", "zzz")]
        self.status(five=99)
        log = (self.home / ".autoswap" / "autoswap.log").read_text()
        blob = log + "".join(o + e for _, o, e in outputs)
        self.assertFalse(any(m in blob for m in SECRET_MARKERS), blob)

    def test_statusline_survives_garbage(self):
        self.two_accounts()
        (self.home / ".autoswap" / "state.json").write_text("{corrupt")
        for stdin in ("not json", "", "[1,2]", '{"rate_limits": "x"}'):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
                self.assertEqual(A.cmd_statusline(None), 0)
            self.assertTrue(out.getvalue().strip())

    def test_keychain_write_uses_stdin_never_argv(self):
        secret = creds("A")
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch.object(A.subprocess, "run", fake):
            A.SecurityKeychain().set("svc", "acct", secret)
        argv = fake.call_args.args[0]
        self.assertEqual(argv, [A.SECURITY, "-i"])
        self.assertIn(secret.encode().hex(), fake.call_args.kwargs["input"])
        self.assertNotIn(secret.encode().hex(), " ".join(argv))

    def test_oversized_secret_is_refused_not_put_in_argv(self):
        fake = mock.Mock()
        with mock.patch.object(A.subprocess, "run", fake):
            with self.assertRaises(A.KeychainError):
                A.SecurityKeychain().set("svc", "acct", "x" * 5000)
        fake.assert_not_called()

    def test_quote_escapes_shell_metacharacters(self):
        self.assertEqual(A._quote('a"b\\c'), '"a\\"b\\\\c"')


class SourceAuditTests(unittest.TestCase):
    """Pins the properties this tool is for: it cannot talk to the network,
    run downloaded code, or spawn anything except security(1) and itself."""

    SRC = (Path(__file__).parent / "autoswap.py").read_text()

    def test_no_network_or_dynamic_code_primitives(self):
        banned = r"\b(urllib|socket|http|https|httpx|requests|ssl|ftplib|smtplib|xmlrpc|asyncio)\b|" \
                 r"(?<![.\w])(eval|exec|compile)\(|\b(__import__|importlib|ctypes|pickle|marshal|base64)\b|" \
                 r"shell\s*=\s*True|os\.system|os\.popen"
        self.assertEqual(re.findall(banned, self.SRC), [])

    def test_only_three_process_spawns(self):
        """security(1) by absolute path, a detached copy of ourselves, and the claude
        executable running its own /usage. Nothing else is ever executed."""
        calls = re.findall(r"subprocess\.(run|Popen|call|check_output|check_call)\(", self.SRC)
        self.assertEqual(sorted(calls), ["Popen", "run", "run"])
        self.assertIn('SECURITY = "/usr/bin/security"', self.SRC)
        self.assertIn('"-p", "/usage", "--no-session-persistence"', self.SRC)

    def test_imports_are_stdlib_only(self):
        mods = set(re.findall(r"^(?:import|from) (\w+)", self.SRC, re.M))
        stdlib = set(sys.stdlib_module_names)
        self.assertEqual(sorted(m for m in mods if m not in stdlib | {"__future__"}), [])


if __name__ == "__main__":
    unittest.main()
