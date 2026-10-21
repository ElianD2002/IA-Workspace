"""Gestión de procesos de terminal con procesos reales (sin GTK)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path

from helpers import temp_dir

from ia_workspace.terminal import process


def wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def is_running(pid: int) -> bool:
    info = process.read_proc(pid)
    return info is not None and info.alive


class ShellAndEnvTests(unittest.TestCase):
    def test_override_shell(self) -> None:
        argv = process.resolve_shell_argv({"IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc"})
        self.assertEqual(argv, ["/bin/bash", "--noprofile", "--norc"])

    def test_shell_from_env_and_fallback(self) -> None:
        self.assertEqual(process.resolve_shell_argv({"SHELL": "/bin/sh"}), ["/bin/sh"])
        argv = process.resolve_shell_argv({"SHELL": "/no/existe"})
        self.assertTrue(os.access(argv[0], os.X_OK))  # pw_shell del usuario o /bin/sh

    def test_env(self) -> None:
        env = dict(
            item.split("=", 1)
            for item in process.build_env(7, "/p", {"HOME": "/h", "TERM": "dumb", "IAW_DATA_DIR": "/x"})
        )
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(env["PWD"], "/p")
        self.assertEqual(env["IAW_PROJECT_ID"], "7")
        self.assertNotIn("TERM", env)
        self.assertNotIn("IAW_DATA_DIR", env)


class ProcParsingTests(unittest.TestCase):
    def test_name_with_spaces_and_parens(self) -> None:
        with temp_dir() as tmp:
            (Path(tmp) / "42").mkdir()
            (Path(tmp) / "42" / "stat").write_text("42 (mi (raro) proc) S 10 42 42 34816 ...\n")
            info = process.read_proc(42, Path(tmp))
            assert info is not None
            self.assertEqual((info.name, info.ppid, info.pgid, info.sid), ("mi (raro) proc", 10, 42, 42))

    def test_members_by_session_and_ancestry(self) -> None:
        procs = [
            process.ProcInfo(100, 1, 100, 100, "S", "bash"),
            process.ProcInfo(101, 100, 101, 100, "S", "sleep"),  # trabajo (otro grupo)
            process.ProcInfo(102, 1, 102, 100, "S", "nohup"),  # huérfano, misma sesión
            process.ProcInfo(103, 101, 103, 103, "S", "daemon"),  # setsid propio, desciende
            process.ProcInfo(104, 100, 100, 100, "Z", "zombie"),
            process.ProcInfo(200, 1, 200, 200, "S", "ajeno"),
        ]
        pids = [p.pid for p in process.session_members(100, procs)]
        self.assertEqual(pids, [100, 101, 102, 103])

    def test_refuses_unsafe_targets(self) -> None:
        with self.assertRaises(ValueError):
            process.signal_session(1, signal.SIGTERM)
        with self.assertRaises(ValueError):
            process.signal_session(os.getpid(), signal.SIGTERM)


class RealProcessTests(unittest.TestCase):
    """Sesiones reales creadas con setsid, como hace VTE."""

    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.cwd = self.tmp.name
        self.spawned: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.spawned:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        self.tmp.cleanup()

    def spawn(self, script: str) -> subprocess.Popen:
        proc = subprocess.Popen(["/bin/sh", "-c", script], cwd=self.cwd, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.append(proc)
        return proc

    def test_kill_session_reaches_jobs_and_signal_ignorers(self) -> None:
        leader = self.spawn(
            "sleep 30 & (trap '' HUP TERM; sleep 30) & nohup sleep 30 >/dev/null 2>&1 & wait"
        )
        bystander = self.spawn("sleep 30")  # otro proceso del usuario, otra sesión
        self.assertTrue(wait_until(lambda: len(process.session_members(leader.pid)) >= 4))
        members = [p.pid for p in process.session_members(leader.pid)]
        self.assertIn(leader.pid, members)
        self.assertTrue(process.has_foreground_work(leader.pid))

        process.signal_session(leader.pid, signal.SIGTERM)
        time.sleep(0.2)
        survivors = [pid for pid in members if is_running(pid)]
        self.assertTrue(survivors, "el proceso que ignora SIGTERM debería seguir vivo")

        process.kill_session(leader.pid)
        leader.wait(timeout=3)
        self.assertTrue(wait_until(lambda: not any(is_running(pid) for pid in members)),
                        f"quedaron procesos: {[pid for pid in members if is_running(pid)]}")
        self.assertTrue(is_running(bystander.pid), "se afectó un proceso ajeno")

    def test_idle_shell_has_no_foreground_work(self) -> None:
        idle = self.spawn("exec sleep 30")  # líder de sesión sin hijos
        self.assertTrue(wait_until(lambda: is_running(idle.pid)))
        self.assertFalse(process.has_foreground_work(idle.pid))


if __name__ == "__main__":
    unittest.main()
