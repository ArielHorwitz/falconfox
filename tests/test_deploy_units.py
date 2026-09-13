"""The deploy scripts, smoke-tested in a temporary HOME.

`deploy/` had no tests at all, which is how `install-units` came to write unit
files into a directory systemd never reads and exit 0 about it. There is no
shell test framework here and does not need to be: these run the real scripts
with `HOME` pointed at a temporary directory and a stand-in `systemctl` on
PATH, so the real user manager is never touched and nothing outside the
temporary directory is written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNITS = ("falconfox-daemon.service", "falconfox-telegram.service")

# A stand-in for the user manager. It answers out of the directory it was told
# systemd has loaded, which is what makes "rendered somewhere systemd does not
# read" a thing a test can stage.
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
set -uo pipefail
loaded="${FAKE_LOADED_DIR:-$HOME/.config/systemd/user}"
arguments=()
for argument in "$@"; do
    [[ "$argument" == "--user" ]] || arguments+=("$argument")
done
case "${arguments[0]}" in
    daemon-reload)
        echo "daemon-reload" >> "$HOME/systemctl.log"
        ;;
    cat)
        cat "$loaded/${arguments[1]}" 2>/dev/null || exit 1
        ;;
    show)
        unit="${arguments[${#arguments[@]}-1]}"
        property=""
        for ((index = 0; index < ${#arguments[@]}; index++)); do
            [[ "${arguments[index]}" == "-p" ]] && property="${arguments[index + 1]}"
        done
        case "$property" in
            FragmentPath)
                [[ -f "$loaded/$unit" ]] && echo "$loaded/$unit" || echo ""
                ;;
            NeedDaemonReload)
                echo "${FAKE_NEEDS_RELOAD:-no}"
                ;;
        esac
        ;;
    is-active)
        exit "${FAKE_INACTIVE:-0}"
        ;;
esac
exit 0
"""


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        binaries = self.home.joinpath("fakebin")
        binaries.mkdir()
        stub = binaries.joinpath("systemctl")
        stub.write_text(FAKE_SYSTEMCTL)
        stub.chmod(0o755)
        self.environment = {
            "HOME": str(self.home),
            "PATH": f"{binaries}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            # The variable that caused the bug: a dev agent session has it set,
            # and it must make no difference to where units are written.
            "XDG_CONFIG_HOME": str(self.home.joinpath("config-elsewhere")),
            "USER": os.environ.get("USER", "tester"),
        }
        self.unit_dir = self.home.joinpath(".config/systemd/user")

    def run_setup(self, *arguments, **environment):
        return subprocess.run(
            ["bash", str(REPO.joinpath("deploy/setup.sh")), *arguments],
            env={**self.environment, **environment},
            capture_output=True, text=True, timeout=60)

    def test_units_land_where_systemd_looks_whatever_the_caller_has_set(self):
        done = self.run_setup("install-units")
        self.assertEqual(done.returncode, 0, done.stderr)
        for unit in UNITS:
            self.assertTrue(self.unit_dir.joinpath(unit).is_file(), unit)
        self.assertFalse(self.home.joinpath("config-elsewhere").exists(),
                         "XDG_CONFIG_HOME is not where the units belong")
        self.assertIn("daemon-reload", self.home.joinpath("systemctl.log").read_text())

    def test_a_rendered_unit_carries_the_repo_and_a_checksum(self):
        self.run_setup("install-units")
        text = self.unit_dir.joinpath("falconfox-daemon.service").read_text()
        self.assertIn(str(REPO), text, "@REPO@ is filled in")
        self.assertIn(str(self.home), text, "@HOME@ is filled in")
        self.assertNotIn("@REPO@", text)
        self.assertRegex(text, r"# falconfox-unit-checksum: [0-9a-f]{16}\n$")

    def test_the_check_passes_on_what_was_just_rendered(self):
        self.run_setup("install-units")
        done = self.run_setup("check-units")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_check_fails_when_systemd_reads_other_text(self):
        # What the old `XDG_CONFIG_HOME` bug left behind: units rendered in one
        # place, systemd serving an older copy from another.
        self.run_setup("install-units")
        stale = self.home.joinpath("stale")
        stale.mkdir()
        for unit in UNITS:
            stale.joinpath(unit).write_text(
                "[Service]\n# falconfox-unit-checksum: 0000000000000000\n")
        done = self.run_setup("check-units", FAKE_LOADED_DIR=str(stale))
        self.assertEqual(done.returncode, 1)
        self.assertIn("other text", done.stderr)

    def test_the_check_fails_when_the_file_has_not_been_loaded(self):
        self.run_setup("install-units")
        done = self.run_setup("check-units", FAKE_NEEDS_RELOAD="yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not been loaded", done.stderr)

    def test_the_check_fails_when_there_are_no_units_at_all(self):
        done = self.run_setup("check-units")
        self.assertEqual(done.returncode, 1)
        self.assertIn("missing", done.stderr)

    def test_the_bootstrap_looks_for_its_config_under_home(self):
        # The bootstrap stops at the first missing config file, before it syncs
        # anything or starts a service, so this drives the real path without
        # deploying anything.
        if shutil.which("uv") is None:
            self.skipTest("the bootstrap checks for uv before anything else")
        done = self.run_setup()
        self.assertEqual(done.returncode, 1)
        self.assertIn(str(self.home.joinpath(".config/falconfox/telegram.env")),
                      done.stderr)
        self.assertNotIn("config-elsewhere", done.stderr)


class ShellSyntaxTests(unittest.TestCase):
    """Every script in deploy/ parses. `update.sh` cannot be driven here -- it
    fast-forwards a checkout and restarts services -- so this is the one thing
    about it that can be checked without deploying."""

    def test_the_scripts_parse(self):
        for script in sorted(REPO.joinpath("deploy").glob("*.sh")):
            with self.subTest(script=script.name):
                done = subprocess.run(["bash", "-n", str(script)],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)


class UpdateHealthCheckTests(unittest.TestCase):
    """A deploy must not call itself healthy without knowing which unit text is
    running. Read out of the script rather than driven: a real run
    fast-forwards a checkout, syncs a venv and restarts the services, none of
    which belongs in a test run."""

    def test_the_health_check_asks_about_the_units(self):
        script = REPO.joinpath("deploy/update.sh").read_text()
        healthy = script.split("healthy() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("check-units", healthy)


class DevUnitTests(unittest.TestCase):
    """`install-dev-units` shares the renderer, so it shares the fix."""

    def test_the_dev_units_are_rendered_into_the_same_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binaries = home.joinpath("fakebin")
            binaries.mkdir()
            stub = binaries.joinpath("systemctl")
            stub.write_text(FAKE_SYSTEMCTL)
            stub.chmod(0o755)
            done = subprocess.run(
                ["bash", str(REPO.joinpath("deploy/setup.sh")), "install-dev-units"],
                env={"HOME": str(home),
                     "PATH": f"{binaries}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                     "XDG_CONFIG_HOME": str(home.joinpath("config-elsewhere"))},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(home.joinpath(
                ".config/systemd/user/falconfox-dev-daemon.service").is_file())
            self.assertFalse(home.joinpath("config-elsewhere").exists())
