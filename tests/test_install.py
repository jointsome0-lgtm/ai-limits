import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


BASH = shutil.which("bash")
INSTALLER = Path(__file__).resolve().parents[1] / "gnome-extension" / "install.sh"
UUID = "ai-limits@jointsome0-lgtm.github.io"


@unittest.skipUnless(sys.platform == "linux" and BASH, "the GNOME installer requires Linux and Bash")
class InstallerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.data = self.tmp / "data with spaces"
        self.env = {**os.environ, "XDG_DATA_HOME": str(self.data)}

    def run_installer(self, *args):
        return subprocess.run([BASH, str(INSTALLER), *args], env=self.env,
                              capture_output=True, encoding="utf-8", timeout=15)

    @unittest.skipUnless(shutil.which("glib-compile-schemas"), "requires GLib schema tools")
    def test_install_and_reinstall_into_xdg_directory(self):
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
        destination = self.data / "gnome-shell" / "extensions" / UUID
        source = INSTALLER.parent / UUID
        for path in source.rglob("*"):
            if path.is_file():
                installed = destination / path.relative_to(source)
                self.assertEqual(installed.read_bytes(), path.read_bytes())
        self.assertTrue((destination / "schemas" / "gschemas.compiled").is_file())
        self.assertIn(str(INSTALLER), result.stdout)

    def test_unexpected_arguments_are_rejected_before_install(self):
        for args in (("--unknown",), ("--enable", "extra")):
            result = self.run_installer(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage:", result.stderr)
        self.assertFalse(self.data.exists())

    def test_missing_tools_are_reported_before_install(self):
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "dirname").symlink_to(shutil.which("dirname"))
        self.env["PATH"] = str(tools)
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        for distro in ("Debian/Ubuntu", "Fedora", "Arch"):
            self.assertIn(distro, result.stderr)
        compiler = tools / "glib-compile-schemas"
        compiler.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        compiler.chmod(0o755)
        result = self.run_installer("--enable")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing gsettings", result.stderr)
        settings = tools / "gsettings"
        settings.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        settings.chmod(0o755)
        result = self.run_installer("--enable")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing python3", result.stderr)
        self.assertFalse(self.data.exists())
