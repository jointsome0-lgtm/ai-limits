import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ai_limits import __version__, cli, config, netguard
from ai_limits.core import build_state
from ai_limits.model import AccountConfig, to_epoch
from ai_limits.providers import claude, codex, private_dir, read_json, write_json


class PortabilityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.tmp), "USERPROFILE": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "CODEX_HOME": str(self.tmp / "codex"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_version_does_not_collect_or_load_config(self):
        with mock.patch.object(cli, "build_state") as build, mock.patch.object(config, "load") as load, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(output.getvalue(), f"ai-limits {__version__}\n")
        build.assert_not_called()
        load.assert_not_called()

    def test_offline_cli_and_tap_without_fcntl_or_getuid(self):
        # A fresh interpreter catches accidental platform-specific imports before main().
        script = '''
import importlib.abc
import io
import os
import sys
class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("fcntl is unavailable")
sys.modules.pop("fcntl", None)
sys.meta_path.insert(0, NoFcntl())
if hasattr(os, "getuid"):
    del os.getuid
from ai_limits.cli import main
assert main(["--version"]) == 0
assert main([]) == 0
assert main(["json"]) == 0
raw = '{"rate_limits":{"five_hour":{"used_percentage":42}},"text":"Привет"}'
sys.stdin = io.StringIO(raw)
assert main(["tap", "claude"]) == 0
assert "fcntl" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONIOENCODING": "ascii"},
                                capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network: off", result.stdout)
        self.assertIn("Привет", result.stdout)
        account = AccountConfig("claude", "Claude", str(self.tmp / "claude"))
        self.assertEqual(read_json(claude.tap_path(account))["rate_limits"]["five_hour"]["used_percentage"], 42)

    def test_non_linux_network_is_refused_before_io_and_preserves_cached_limits(self):
        account = AccountConfig("claude", "Claude", str(self.tmp / "claude"))
        now = int(time.time())
        claude.write_tap(account.home, {"rate_limits": {"five_hour": {"used_percentage": 42}}}, now)
        write_json(config.cache_dir() / "net" / f"{account.id}.json", {
            "attempted_at": now, "fetched_at": now,
            "limits": [{"id": "7d", "label": "Week", "used_percent": 75}],
        })
        cfg = config.Config(accounts=[account], network=config.NetworkConfig(True, "tun0"))
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                 mock.patch.object(claude, "read_json", wraps=read_json) as read, \
                 mock.patch.object(claude, "_fetch_locked") as fetch, \
                 mock.patch.object(claude, "private_dir") as mkdir, \
                 mock.patch("socket.getaddrinfo") as dns, mock.patch("socket.socket") as sock, \
                 mock.patch("subprocess.run") as run:
                state = build_state(cfg, now)
                self.assertFalse(state["network"]["vpn_ok"])
                result = state["accounts"][0]
                self.assertEqual(result["status"], "vpn_off")
                self.assertIn("requires Linux", result["message"])
                self.assertEqual({lim["id"] for lim in result["limits"]}, {"5h", "7d"})
                self.assertFalse(any(".credentials" in str(call.args[0]) for call in read.call_args_list))
                for action in (fetch, mkdir, dns, sock, run):
                    action.assert_not_called()

    def test_direct_network_entry_points_are_also_fail_closed(self):
        # Construct using the real host's TLS defaults before emulating another platform.
        connection = netguard._PinnedHTTPSConnection("h", "192.0.2.1", "tun0")
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                 mock.patch("socket.getaddrinfo") as dns, mock.patch.object(netguard, "_get") as get:
                with self.assertRaisesRegex(netguard.VpnCheckFailed, "requires Linux"):
                    netguard.get_json_authenticated("h", "/usage", {"Authorization": "fake"}, "tun0",
                                                    probe_expect=claude.PROBE_EXPECT)
                with mock.patch("socket.socket") as sock:
                    with self.assertRaisesRegex(netguard.VpnCheckFailed, "requires Linux"):
                        connection.connect()
                for action in (dns, get, sock):
                    action.assert_not_called()

    def test_linux_without_bind_to_device_is_refused_before_dns(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(netguard, "socket") as sockets:
            del sockets.SO_BINDTODEVICE
            with self.assertRaisesRegex(netguard.VpnCheckFailed, "SO_BINDTODEVICE"):
                netguard.check_route("h", "tun0")
        sockets.getaddrinfo.assert_not_called()

    def test_unsuccessful_route_command_is_refused(self):
        with mock.patch.object(netguard, "require_supported_platform"), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("192.0.2.1", 443))]), \
             mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ip")) as run:
            with self.assertRaisesRegex(netguard.VpnCheckFailed, "cannot check route"):
                netguard.check_route("h", "tun0")
        self.assertTrue(run.call_args.kwargs["check"])

    def test_cache_write_is_atomic(self):
        target = self.tmp / "cache" / "state.json"
        write_json(target, {"label": "old"})
        write_json(target, {"label": "Привет · 東京"})
        private_dir(target.parent)
        self.assertEqual(read_json(target), {"label": "Привет · 東京"})
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_utf8_config_and_json_do_not_depend_on_locale(self):
        path = self.tmp / "config.toml"
        path.write_text('[[account]]\nprovider = "codex"\nlabel = "東京 · Привет"\nhome = "./data"\n', encoding="utf-8")
        self.assertEqual(config.load(path).accounts[0].label, "東京 · Привет")
        data = self.tmp / "source.json"
        data.write_text('{"label": "東京 · Привет"}', encoding="utf-8")
        self.assertEqual(read_json(data), {"label": "東京 · Привет"})
        data.write_bytes(b'\xff')
        self.assertIsNone(read_json(data))
        path.write_bytes(b'\xff')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_timestamp_offsets_and_naive_utc(self):
        original_timezone = os.environ.get("TZ")
        os.environ["TZ"] = "UTC+11"
        if hasattr(time, "tzset"):
            time.tzset()
        try:
            for value in ("2026-10-27T12:00:00Z", "2026-10-27T15:00:00+03:00", "2026-10-27T12:00:00"):
                self.assertEqual(to_epoch(value), 1793102400)
                self.assertEqual(codex._parse_ts(value), 1793102400)
            self.assertEqual(codex._parse_ts("2026-10-27T12:00:00.5Z"), 1793102400.5)
        finally:
            if original_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_timezone
            if hasattr(time, "tzset"):
                time.tzset()

    def test_out_of_range_timestamps_do_not_crash(self):
        self.assertIsNone(to_epoch(10 ** 1000))
        self.assertIsNone(codex._parse_ts("not a timestamp"))
        self.assertEqual(cli._reset_text(10 ** 100, int(time.time())), "reset time out of range")
