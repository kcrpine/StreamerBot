"""The virtual display, and the environment Chrome is launched with.

Netflix, Disney Plus, Apple Music and Amazon Music need a headful Chrome, which
needs an X display. Two faults between them meant those four services were dead
on any bot container that had ever been restarted, and neither said so.

Measured against the shipped entrypoint, restarting one container four times:

    run 1 (fresh start)  display OK       Xvfb running
    run 2 (restart)      DISPLAY MISSING  no Xvfb
    run 3 (restart)      DISPLAY MISSING  no Xvfb
    run 4 (restart)      DISPLAY MISSING  no Xvfb

and the entrypoint printed "OK. Virtual display :99 started" on all four,
including the three failures. Bot containers are created with --restart always,
so in practice every bot was in that state.

The cause is /tmp surviving a docker restart. Xvfb's lock file still holds the
previous run's pid, and after a restart that low pid is alive again in the fresh
pid namespace, so Xvfb decides a server is genuinely running and exits. It is not
the stale-lock case Xvfb cleans up by itself, which is why a lock naming a dead
pid does not reproduce it.
"""

import re
import unittest
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]


def read_entrypoint():
    path = ROOT / "entrypoint.sh"
    if not path.is_file():
        raise unittest.SkipTest("entrypoint.sh is not present; host-only test skipped")
    return path.read_text(encoding="utf-8", errors="replace")


class DisplayStartupTests(TestCase):
    def setUp(self):
        self.script = read_entrypoint()

    def test_a_stale_lock_is_cleared_before_starting_xvfb(self):
        self.assertRegex(self.script, r"\.X\$\{display_number\}-lock")

    def test_the_lock_is_only_cleared_when_no_server_answers(self):
        """Clearing unconditionally would pull the display out from under a
        server that is genuinely running."""
        block = self.script.split("display_number=", 1)[1].split("Xvfb \"$DISPLAY\"", 1)[0]
        self.assertIn("if ! xdpyinfo", block)
        # The removal must sit inside that guard.
        guard = block.split("if ! xdpyinfo", 1)[1]
        self.assertIn("rm -f", guard.split("fi", 1)[0])

    def test_the_socket_is_cleared_too_not_just_the_lock(self):
        self.assertIn(".X11-unix/X${display_number}", self.script)

    def test_success_is_reported_only_when_the_display_answers(self):
        """The original printed OK unconditionally after the wait loop, so it
        reported success having failed every check. A start-up line that is
        always OK is worse than none: it points the reader away from the fault."""
        self.assertIn("display_ready=1", self.script)
        ok_line = [
            line for line in self.script.splitlines()
            if "Virtual display" in line and "OK." in line
        ]
        self.assertEqual(len(ok_line), 1, ok_line)
        # It has to be guarded by the readiness flag.
        before = self.script.split(ok_line[0], 1)[0]
        self.assertRegex(before.rsplit("\n", 4)[-4:][0] + "\n".join(before.rsplit("\n", 4)[-3:]),
                         r'if \[ "\$display_ready" = "1" \]')

    def test_failure_names_the_services_that_will_not_work(self):
        """"Xvfb failed" means nothing to the person whose Apple Music sign-in
        just died."""
        self.assertRegex(
            self.script,
            r"Error\. The virtual display .* did not start[\s\S]{0,200}Apple Music",
        )

    def test_failure_shows_what_xvfb_said(self):
        self.assertIn("/tmp/xvfb.log", self.script)

    def test_the_display_number_is_derived_not_hardcoded(self):
        """DISPLAY is overridable, so :99 must not be baked into the paths."""
        self.assertIn('display_number="${DISPLAY#:}"', self.script)
        lock_lines = [ln for ln in self.script.splitlines() if "-lock" in ln]
        for line in lock_lines:
            self.assertNotIn(".X99-lock", line, line)


class ChromeEnvironmentTests(TestCase):
    """Playwright *replaces* the environment when given env=.

    So env={"DISPLAY": ...} launched Chrome with no HOME, no XDG_RUNTIME_DIR and
    no PULSE_*. A Chrome that cannot find the PulseAudio socket plays into
    nothing, which is indistinguishable from a site that failed to start the
    video — and it would have been the next fault after the display one.
    """

    def setUp(self):
        try:
            from bot.player.engines.browser_engine import BrowserEngine
        except Exception as error:  # pragma: no cover - host without deps
            raise unittest.SkipTest(f"browser_engine is not importable here: {error}")
        self.BrowserEngine = BrowserEngine

    def launch_env(self, tmp_dir):
        engine = object.__new__(self.BrowserEngine)
        engine._dir = tmp_dir
        engine._display = ":99"
        engine._contexts = {}
        engine._playwright = MagicMock()
        engine._context_for("am")
        call = engine._playwright.chromium.launch_persistent_context.call_args
        return call.kwargs["env"]

    def test_the_real_environment_is_carried_through(self):
        import os
        import tempfile

        env = self.launch_env(tempfile.mkdtemp())

        self.assertEqual(env["DISPLAY"], ":99")
        # A couple of things Chrome and PulseAudio both need.
        for name in ("PATH", "HOME"):
            if name in os.environ:
                self.assertIn(name, env, f"{name} was dropped from Chrome's environment")

    def test_pulse_variables_survive(self):
        """The whole point of the merge: audio has to reach the null sink."""
        import os
        import tempfile

        os.environ["PULSE_SINK"] = "StreamerBotSink"
        try:
            env = self.launch_env(tempfile.mkdtemp())
            self.assertEqual(env.get("PULSE_SINK"), "StreamerBotSink")
        finally:
            os.environ.pop("PULSE_SINK", None)

    def test_display_still_wins_over_an_inherited_one(self):
        """A DISPLAY inherited from the host must not override the engine's."""
        import os
        import tempfile

        os.environ["DISPLAY"] = ":0"
        try:
            env = self.launch_env(tempfile.mkdtemp())
            self.assertEqual(env["DISPLAY"], ":99")
        finally:
            os.environ.pop("DISPLAY", None)


if __name__ == "__main__":
    unittest.main()
