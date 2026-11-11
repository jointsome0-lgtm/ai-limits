import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_limits import config, netguard
from ai_limits.core import build_state
from ai_limits.model import AccountConfig, Limit, roll_forward
from ai_limits.providers import claude, gemini

NOW = 1_793_107_651
TOKEN = "sk-ant-oat01-" + "x" * 40  # fake, but the right shape
linux_network = unittest.skipUnless(sys.platform == "linux", "network mode is Linux-only")


def codex_record(ts, used, resets, limit_id="codex", window=10080):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": limit_id, "limit_name": None, "plan_type": "pro", "secondary": None,
        "primary": {"used_percent": used, "window_minutes": window, "resets_at": resets}}}})


class Env(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache"),
                                               "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
                                               "HOME": str(self.tmp), "USERPROFILE": str(self.tmp),
                                               "CLAUDE_CONFIG_DIR": str(self.tmp / ".claude"),
                                               "CODEX_HOME": str(self.tmp / ".codex")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def codex_home(self, name, lines):
        home = self.tmp / name
        day = home / "sessions/2026/10/27"
        day.mkdir(parents=True)
        (day / "rollout-a.jsonl").write_text("\n".join(lines) + "\n")
        return str(home)

    def cfg(self, *accounts, network=None):
        return config.Config(accounts=list(accounts), network=network or config.NetworkConfig())


class CodexTest(Env):
    def test_newest_snapshot_wins_and_plan_is_read(self):
        home = self.codex_home("c", [
            codex_record("2026-10-27T10:00:00.000Z", 50.0, NOW + 1000),
            '{"broken',
            codex_record("2026-10-27T12:00:00.000Z", 64.0, NOW + 1000),
            codex_record("2026-10-27T11:00:00.000Z", 60.0, NOW + 1000),
        ])
        acc = build_state(self.cfg(AccountConfig("codex", "Codex", home)), NOW)["accounts"][0]
        self.assertEqual(acc["plan"], "pro")
        self.assertEqual([(lim["id"], lim["used_percent"]) for lim in acc["limits"]], [("7d", 64.0)])

    def test_extra_limit_ids_get_their_own_rows(self):
        home = self.codex_home("c", [
            codex_record("2026-10-27T12:00:00.000Z", 64.0, NOW + 1000),
            codex_record("2026-10-27T12:00:01.000Z", 10.0, NOW + 1000, limit_id="gpt-6-astra", window=300),
        ])
        acc = build_state(self.cfg(AccountConfig("codex", "Codex", home)), NOW)["accounts"][0]
        self.assertEqual([lim["id"] for lim in acc["limits"]], ["7d", "5h:gpt-6-astra"])

    def test_no_sessions(self):
        home = self.tmp / "empty"
        home.mkdir()
        acc = build_state(self.cfg(AccountConfig("codex", "Codex", str(home))), NOW)["accounts"][0]
        self.assertEqual(acc["status"], "no_data")


class StateTest(Env):
    def test_old_high_limit_still_counts_for_recommendation(self):
        a = self.codex_home("a", [codex_record("2026-10-27T05:00:00.000Z", 99.0, NOW + 400000),
                                  codex_record("2026-10-27T12:00:00.000Z", 5.0, NOW + 1000, limit_id="gpt-6-astra", window=300)])
        b = self.codex_home("b", [codex_record("2026-10-27T12:00:00.000Z", 50.0, NOW + 400000)])
        state = build_state(self.cfg(AccountConfig("codex", "A", a), AccountConfig("codex", "B", b)), NOW)
        self.assertEqual({x["label"]: x["recommended"] for x in state["accounts"]}, {"A": False, "B": True})

    def test_recommended_is_account_with_most_headroom(self):
        busy = self.codex_home("busy", [codex_record("2026-10-27T12:00:00.000Z", 91.0, NOW + 1000)])
        free = self.codex_home("free", [codex_record("2026-10-27T12:00:00.000Z", 19.0, NOW + 1000)])
        state = build_state(self.cfg(AccountConfig("codex", "work", busy), AccountConfig("codex", "me", free)), NOW)
        self.assertEqual({a["label"]: a["recommended"] for a in state["accounts"]}, {"work": False, "me": True})

    def test_single_account_is_not_marked(self):
        home = self.codex_home("c", [codex_record("2026-10-27T12:00:00.000Z", 19.0, NOW + 1000)])
        self.assertFalse(build_state(self.cfg(AccountConfig("codex", "me", home)), NOW)["accounts"][0]["recommended"])

    def test_old_data_is_stale(self):
        home = self.codex_home("c", [codex_record("2026-10-26T00:00:00.000Z", 19.0, NOW + 100000)])
        acc = build_state(self.cfg(AccountConfig("codex", "me", home)), NOW)["accounts"][0]
        self.assertEqual(acc["status"], "stale")
        self.assertEqual(acc["limits"][0]["used_percent"], 19.0)

    def test_roll_forward_after_reset(self):
        lim = roll_forward(Limit("5h", "S", 80.0, NOW - 60, 300), NOW)
        self.assertEqual((lim.used_percent, lim.estimated), (0.0, True))
        self.assertEqual(lim.resets_at, NOW - 60 + 300 * 60)


class ClaudeTest(Env):
    def test_tap_round_trip_uses_same_key_as_statusline(self):
        home = str(self.tmp / ".claude")
        claude.write_tap(home, {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": NOW + 600},
                                                "seven_day": {"used_percentage": 18, "resets_at": "2026-11-01T16:00:00Z"}}}, NOW)
        acc = build_state(self.cfg(AccountConfig("claude", "Claude", home)), NOW)["accounts"][0]
        self.assertEqual([(lim["id"], lim["used_percent"]) for lim in acc["limits"]], [("5h", 42.0), ("7d", 18.0)])
        self.assertEqual(acc["source"], "tap")

    def test_offline_mode_never_touches_network_or_credentials(self):
        home = self.tmp / ".claude"
        home.mkdir()
        with mock.patch.object(netguard, "_get") as get, mock.patch.object(claude, "read_json", wraps=claude.read_json) as rj:
            build_state(self.cfg(AccountConfig("claude", "Claude", str(home))), NOW)
        get.assert_not_called()
        self.assertFalse(any(".credentials" in str(c.args[0]) for c in rj.call_args_list))

    def test_network_needs_an_interface(self):
        self.assertFalse(config.NetworkConfig(enabled=True).active)

    def test_vpn_down_sends_nothing(self):
        home = self.tmp / ".claude"
        home.mkdir()
        (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": (NOW + 3600) * 1000}}))
        net = config.NetworkConfig(enabled=True, require_interface="definitely-not-an-iface0")
        with mock.patch("socket.getaddrinfo") as dns, mock.patch("socket.create_connection") as conn:
            acc = build_state(self.cfg(AccountConfig("claude", "Claude", str(home)), network=net), NOW)["accounts"][0]
        dns.assert_not_called()
        conn.assert_not_called()
        self.assertEqual(acc["status"], "vpn_off")

    @linux_network
    def test_route_through_other_interface_is_refused(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 443))]), \
             mock.patch("subprocess.run") as run:
            run.return_value.stdout = "1.2.3.4 via 192.0.2.1 dev wlan0 src 192.0.2.40 uid 1000"
            with self.assertRaises(netguard.VpnCheckFailed):
                netguard.check_route("api.anthropic.com", "tun0")

    def _get_json_with_probe(self, probe_answer):
        calls = []

        def fake_get(host, ip, interface, path, headers, timeout):
            calls.append(dict(headers))
            return probe_answer if not headers else (200, b'{"limits": []}')

        with mock.patch.object(netguard, "check_route", return_value="1.2.3.4"), \
             mock.patch.object(netguard, "_get", side_effect=fake_get):
            try:
                netguard.get_json_authenticated("api.anthropic.com", "/u", {"Authorization": "Bearer t"}, "tun0",
                                                probe_expect=claude.PROBE_EXPECT)
                return calls, None
            except netguard.VpnCheckFailed as e:
                return calls, e

    def test_region_block_stops_before_token_is_sent(self):
        calls, err = self._get_json_with_probe((403, b'{"error": {"type": "forbidden"}}'))
        self.assertIsNotNone(err)
        self.assertEqual(calls, [{}])  # only the anonymous probe went out

    def test_unexpected_probe_answer_also_stops(self):
        for answer in [(200, b"{}"), (401, b"<html>"), (302, b"")]:
            calls, err = self._get_json_with_probe(answer)
            self.assertIsNotNone(err, answer)
            self.assertEqual(calls, [{}])

    def test_expected_probe_lets_real_request_through(self):
        calls, err = self._get_json_with_probe((401, b'{"error": {"type": "authentication_error"}}'))
        self.assertIsNone(err)
        self.assertEqual(calls, [{}, {"Authorization": "Bearer t"}])

    def test_probe_is_mandatory_for_credentials(self):
        with mock.patch.object(netguard, "_get") as get:
            with self.assertRaises(ValueError):
                netguard.get_json_authenticated("h", "/u", {"Authorization": "x"}, "tun0", probe_expect=None)
        get.assert_not_called()

    @linux_network
    def test_sockets_are_bound_to_the_vpn_interface(self):
        conn = netguard._PinnedHTTPSConnection("h", "1.2.3.4", "definitely-not-an-iface0", timeout=1)
        with mock.patch("socket.socket.connect") as connect:
            with self.assertRaises(OSError):  # ENODEV from SO_BINDTODEVICE, before any connect
                conn.connect()
        connect.assert_not_called()

    def _net_account(self, token):
        home = self.tmp / ".claude"
        home.mkdir(exist_ok=True)
        (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": (NOW + 3600) * 1000}}))
        net = config.NetworkConfig(enabled=True, require_interface="tun0")
        return self.cfg(AccountConfig("claude", "Claude", str(home)), network=net)

    @linux_network
    def test_malformed_token_is_never_sent_or_echoed(self):
        bad = TOKEN + "\nX-Evil: 1"
        with mock.patch.object(netguard, "get_json_authenticated") as get:
            state = build_state(self._net_account(bad), NOW)
        get.assert_not_called()
        self.assertNotIn(TOKEN, json.dumps(state))

    @linux_network
    def test_request_errors_do_not_leak_the_token(self):
        with mock.patch.object(netguard, "get_json_authenticated", side_effect=ValueError(f"Invalid header b'Bearer {TOKEN}'")):
            state = build_state(self._net_account(TOKEN), NOW)
        self.assertEqual(state["accounts"][0]["status"], "error")
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertNotIn(TOKEN, (self.tmp / "cache/ai-limits/net").joinpath(f"{state['accounts'][0]['id']}.json").read_text())

    @linux_network
    def test_vpn_ok_reflects_this_run_only(self):
        with mock.patch.object(netguard, "check_route", return_value="1.2.3.4"), \
             mock.patch.object(netguard, "_get", side_effect=[(401, b'{"error": {"type": "authentication_error"}}'), (200, b'{"limits": []}')]):
            self.assertIs(build_state(self._net_account(TOKEN), NOW)["network"]["vpn_ok"], True)
        # second run within min_interval: served from cache, no check made
        self.assertIsNone(build_state(self._net_account(TOKEN), NOW + 10)["network"]["vpn_ok"])

    def test_newer_partial_network_keeps_fresh_tap_limits(self):
        home = str(self.tmp / ".claude")
        claude.write_tap(home, {"rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": NOW + 600}}}, NOW - 30)
        net = claude._parse_usage({"limits": [{"kind": "weekly_scoped", "percent": 5, "resets_at": NOW + 9000,
                                               "scope": {"model": {"display_name": "Fable"}}}]}, NOW)
        with mock.patch.object(claude, "_from_network", return_value=net):
            cfg = self.cfg(AccountConfig("claude", "Claude", home), network=config.NetworkConfig(True, "tun0"))
            acc = build_state(cfg, NOW)["accounts"][0]
        self.assertEqual([lim["id"] for lim in acc["limits"]], ["5h", "7d:fable"])

    def test_old_network_extras_make_account_stale_not_fresh(self):
        home = str(self.tmp / ".claude")
        claude.write_tap(home, {"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": NOW + 600}}}, NOW)
        net = claude._parse_usage({"limits": [{"kind": "weekly_scoped", "percent": 99, "resets_at": NOW + 9000,
                                               "scope": {"model": {"display_name": "Fable"}}}]}, NOW - 86400)
        with mock.patch.object(claude, "_from_network", return_value=net):
            cfg = self.cfg(AccountConfig("claude", "Claude", home), network=config.NetworkConfig(True, "tun0"))
            acc = build_state(cfg, NOW)["accounts"][0]
        self.assertEqual([lim["id"] for lim in acc["limits"]], ["5h", "7d:fable"])  # kept: 99 % still constrains
        self.assertEqual(acc["updated_at"], NOW - 86400)
        self.assertEqual(acc["status"], "stale")

    @linux_network
    def test_nan_sibling_does_not_wipe_valid_limits(self):
        with mock.patch.object(netguard, "get_json_authenticated", return_value={"limits": [
                {"kind": "session", "percent": 80, "resets_at": NOW + 600},
                {"kind": "weekly_all", "percent": float("nan")}]}):
            acc = build_state(self._net_account(TOKEN), NOW)["accounts"][0]
        self.assertEqual((acc["status"], [lim["id"] for lim in acc["limits"]]), ("ok", ["5h"]))
        # and the cached copy reads back the same
        self.assertEqual([lim["id"] for lim in build_state(self._net_account(TOKEN), NOW + 10)["accounts"][0]["limits"]], ["5h"])

    @linux_network
    def test_http_parser_errors_do_not_leak_the_token(self):
        import http.client
        with mock.patch.object(netguard, "get_json_authenticated", side_effect=http.client.BadStatusLine(f"Bearer {TOKEN}")):
            state = build_state(self._net_account(TOKEN), NOW)
        self.assertNotIn(TOKEN, json.dumps(state))

    @linux_network
    def test_malformed_probe_counts_as_failed_check(self):
        import http.client
        with mock.patch.object(netguard, "check_route", return_value="1.2.3.4"), \
             mock.patch.object(netguard, "_get", side_effect=http.client.BadStatusLine("x")) as get, \
             mock.patch("time.sleep"):
            state = build_state(self._net_account(TOKEN), NOW)
        self.assertEqual((state["network"]["vpn_ok"], state["accounts"][0]["status"]), (False, "vpn_off"))
        self.assertTrue(all(not c.args[4] for c in get.call_args_list))  # only anonymous calls were made

    def test_nan_percentages_are_dropped(self):
        snap = claude._parse_usage({"limits": [{"kind": "session", "percent": float("nan")},
                                               {"kind": "weekly_all", "percent": "50"}]}, NOW)
        self.assertEqual(snap.limits, [])

    def test_network_parse_adds_per_model_week(self):
        usage = {"limits": [
            {"kind": "session", "percent": 5, "resets_at": "2026-10-27T13:30:00.498150+00:00"},
            {"kind": "weekly_all", "percent": 20, "resets_at": "2026-11-01T16:00:00+00:00"},
            {"kind": "weekly_scoped", "percent": 0, "resets_at": "2026-11-01T16:00:00+00:00",
             "scope": {"model": {"display_name": "Fable"}}},
        ]}
        snap = claude._parse_usage(usage, NOW)
        self.assertEqual([lim.id for lim in snap.limits], ["5h", "7d", "7d:fable"])


class LeakTest(Env):
    def test_cli_typo_collects_nothing(self):
        from ai_limits import cli
        with mock.patch.object(cli, "build_state") as build, mock.patch.object(config, "load") as load, \
             mock.patch("sys.stderr"):
            for argv in (["--version", "extra"], ["--offline"], ["jsn"], ["json", "extra"]):
                self.assertEqual(cli.main(argv), 2, argv)
        build.assert_not_called()
        load.assert_not_called()

    def test_tls_key_logging_is_off(self):
        with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(self.tmp / "keys.log")}):
            self.assertIsNone(netguard._tls_context().keylog_filename)

    @linux_network
    def test_route_check_runs_with_minimal_env(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 443))]), \
             mock.patch("subprocess.run") as run, mock.patch.dict(os.environ, {"SECRET": "x"}):
            run.return_value.stdout = "1.2.3.4 dev tun0 src 10.0.0.1"
            netguard.check_route("h", "tun0")
        self.assertEqual(run.call_args.kwargs["env"], netguard.SUBPROCESS_ENV)

    def test_tap_keeps_only_limit_fields(self):
        home = str(self.tmp / ".claude")
        claude.write_tap(home, {"workspace": {"current_dir": "/secret/project"}, "rate_limits": {
            "five_hour": {"used_percentage": 1, "resets_at": 2, "future_field": "x"}, "other": {"a": 1}}}, NOW)
        text = claude.tap_path(AccountConfig("claude", "C", home)).read_text()
        self.assertNotIn("secret", text)
        self.assertNotIn("future_field", text)
        self.assertNotIn("other", text)

    @unittest.skipUnless(os.name == "posix", "Windows uses inherited ACLs, not POSIX permissions")
    def test_cache_files_and_dirs_are_private(self):
        from ai_limits.providers import write_json
        target = self.tmp / "cache/ai-limits/net/x.json"
        write_json(target, {"a": 1})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["x.json"])  # no temp leftovers

    @linux_network
    def test_concurrent_runs_fetch_once(self):
        import fcntl
        cfg = ClaudeTest._net_account(self, TOKEN)
        acc = cfg.accounts[0]
        net_dir = self.tmp / "cache/ai-limits/net"
        net_dir.mkdir(parents=True)
        with open(net_dir / f"{acc.id}.lock", "a") as held, \
             mock.patch.object(netguard, "get_json_authenticated") as get:
            fcntl.flock(held, fcntl.LOCK_EX)  # another process is fetching right now
            build_state(cfg, NOW)
        get.assert_not_called()

    @linux_network
    def test_attempt_is_recorded_before_the_request(self):
        cfg = ClaudeTest._net_account(self, TOKEN)
        seen = {}

        def fake(*a, **kw):
            path = self.tmp / f"cache/ai-limits/net/{cfg.accounts[0].id}.json"
            seen["attempted_at"] = json.loads(path.read_text()).get("attempted_at")
            raise KeyboardInterrupt  # the extension killed us mid-request

        with mock.patch.object(netguard, "get_json_authenticated", side_effect=fake):
            with self.assertRaises(KeyboardInterrupt):
                build_state(cfg, NOW)
        self.assertEqual(seen["attempted_at"], NOW)


class CodexTimeTest(Env):
    def test_timestamps_compare_as_times_not_strings(self):
        home = self.codex_home("c", [
            codex_record("2026-10-27T12:00:00.500Z", 70.0, NOW + 1000),  # newer
            codex_record("2026-10-27T12:00:00Z", 60.0, NOW + 1000),      # sorts after it as a string
        ])
        acc = build_state(self.cfg(AccountConfig("codex", "Codex", home)), NOW)["accounts"][0]
        self.assertEqual(acc["limits"][0]["used_percent"], 70.0)


def agy_input(email, fraction, plan="Ultra", reset_time="2026-11-01T16:00:00Z"):
    return {"email": email, "plan_tier": plan, "workspace": {"current_dir": "/secret/project"},
            "quota": {"gemini-weekly": {"remaining_fraction": fraction, "reset_time": reset_time,
                                        "reset_in_seconds": 600}}}


class GeminiTest(Env):
    def test_tap_round_trip(self):
        gemini.write_tap(agy_input("A@gmail.com", 0.9378), NOW)
        acc = build_state(self.cfg(AccountConfig("gemini", "G", "/x", "a@gmail.com")), NOW)["accounts"][0]
        self.assertEqual([(lim["id"], lim["label"]) for lim in acc["limits"]], [("7d", "Week")])
        self.assertAlmostEqual(acc["limits"][0]["used_percent"], 6.22)
        self.assertEqual((acc["source"], acc["plan"], acc["status"]), ("tap", "Ultra", "ok"))

    def test_two_accounts_are_kept_apart_and_recommended(self):
        gemini.write_tap(agy_input("x5@gmail.com", 0.2), NOW)
        gemini.write_tap(agy_input("x20@gmail.com", 0.7), NOW)
        state = build_state(self.cfg(AccountConfig("gemini", "Ultra x5", "/x", "x5@gmail.com"),
                                     AccountConfig("gemini", "Ultra x20", "/x", "x20@gmail.com")), NOW)
        used = {a["label"]: (round(a["limits"][0]["used_percent"]), a["recommended"]) for a in state["accounts"]}
        self.assertEqual(used, {"Ultra x5": (80, False), "Ultra x20": (30, True)})

    def test_tap_keeps_no_email_or_paths(self):
        gemini.write_tap(agy_input("secret@gmail.com", 0.5), NOW)
        text = gemini.tap_path(gemini.email_key("secret@gmail.com")).read_text()
        self.assertNotIn("secret", text)
        self.assertNotIn("/secret/project", text)

    def test_reset_from_seconds_and_other_buckets(self):
        gemini.write_tap({"quota": {"gemini-pro-daily": {"remaining_fraction": 0.25, "reset_in_seconds": 600},
                                    "flash": {"remaining_amount": 3},
                                    "gemini-weekly": {"remaining_fraction": 1, "disabled": True}}}, NOW)
        acc = build_state(self.cfg(AccountConfig("gemini", "G", "/x")), NOW)["accounts"][0]
        self.assertEqual([(lim["id"], lim["label"], lim["used_percent"], lim["resets_at"]) for lim in acc["limits"]],
                         [("1d:pro", "Pro (day)", 75.0, NOW + 600)])

    def test_unknown_email_has_no_data(self):
        gemini.write_tap(agy_input("a@gmail.com", 0.5), NOW)
        acc = build_state(self.cfg(AccountConfig("gemini", "G", "/x", "b@gmail.com")), NOW)["accounts"][0]
        self.assertEqual(acc["status"], "no_data")


class ConfigTest(Env):
    def test_gemini_accounts_by_email(self):
        path = self.tmp / "config.toml"
        path.write_text('[[account]]\nprovider = "gemini"\nlabel = "x5"\nemail = "a@gmail.com"\n'
                        '[[account]]\nprovider = "gemini"\nlabel = "x20"\nemail = "b@gmail.com"\n')
        cfg = config.load(path)
        self.assertEqual([(a.label, a.email) for a in cfg.accounts], [("x5", "a@gmail.com"), ("x20", "b@gmail.com")])
        path.write_text('[[account]]\nprovider = "gemini"\n[[account]]\nprovider = "gemini"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)
        path.write_text('[[account]]\nprovider = "codex"\nemail = "a@gmail.com"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_accounts_from_file(self):
        path = self.tmp / "config.toml"
        path.write_text('[[account]]\nprovider = "codex"\nlabel = "work"\nhome = "~/.codex-work"\n')
        cfg = config.load(path)
        self.assertEqual([(a.provider, a.label) for a in cfg.accounts], [("codex", "work")])
        self.assertFalse(cfg.network.active)

    def test_string_false_does_not_enable_network(self):
        path = self.tmp / "config.toml"
        path.write_text('[network]\nenabled = "false"\nrequire_interface = "tun0"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_duplicate_accounts_rejected(self):
        path = self.tmp / "config.toml"
        path.write_text('[[account]]\nprovider = "codex"\nhome = "~/.codex"\n[[account]]\nprovider = "codex"\nhome = "~/.codex/"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_bad_values_rejected(self):
        for text in ["stale_after_hours = nan\n", "account = [1]\n", "[network]\nmin_interval_seconds = 5\n"]:
            path = self.tmp / "config.toml"
            path.write_text(text)
            with self.assertRaises(config.ConfigError, msg=text):
                config.load(path)

    def test_bad_provider(self):
        path = self.tmp / "config.toml"
        path.write_text('[[account]]\nprovider = "bard"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)


if __name__ == "__main__":
    unittest.main()
