"""Integración con agentes oficiales: lanzamiento, autenticación y catálogo (sin GTK).

Todo con dobles: ninguna prueba ejecuta Claude Code, Codex ni Antigravity reales.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from ia_workspace.agents import auth, tools
from ia_workspace.agents.auth import AuthState, CompletedCommand
from ia_workspace.agents.catalog import AgentCatalog
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind

FAKE_PATHS = {"claude": "/fake/bin/claude", "codex": "/fake/bin/codex", "agy": "/fake/bin/agy"}
PRIVATE = ("persona@example.invalid", "FAKE-ORG-SECRET-123")


def which_with(*names: str):
    return lambda name: FAKE_PATHS.get(name) if name in names else None


class NoRun:
    """Runner que falla si se ejecuta algo: comprueba que no se lanzan procesos."""

    def __call__(self, argv):  # pragma: no cover - solo se ejecuta si hay un error
        raise AssertionError(f"no debería ejecutarse: {argv}")


class LaunchTests(unittest.TestCase):
    def test_interactive_commands_are_the_official_ones(self) -> None:
        which = which_with("claude", "codex", "agy")
        self.assertEqual(tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=which), ["/fake/bin/claude"])
        self.assertEqual(tools.build_launch_argv(AgentKind.CODEX, which=which),
                         ["/fake/bin/codex", "--no-daemon"])
        self.assertEqual(tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=which), ["/fake/bin/agy"])

    def test_login_commands(self) -> None:
        which = which_with("claude", "codex", "agy")
        self.assertEqual(tools.build_login_argv(AgentKind.CLAUDE_CODE, which=which),
                         ["/fake/bin/claude", "auth", "login"])
        self.assertEqual(tools.build_login_argv(AgentKind.CODEX, which=which), ["/fake/bin/codex", "login"])
        with self.assertRaises(ValueError):
            tools.build_login_argv(AgentKind.ANTIGRAVITY, which=which)

    def test_antigravity_falls_back_to_agy(self) -> None:
        only_agy = which_with("agy")
        self.assertEqual(tools.TOOLS[AgentKind.ANTIGRAVITY].find(only_agy), "/fake/bin/agy")

    def test_missing_tool(self) -> None:
        with self.assertRaises(tools.ToolNotInstalledError) as caught:
            tools.build_launch_argv(AgentKind.CODEX, which=which_with())
        self.assertIn("Codex no está instalado", str(caught.exception))

    def test_never_bypasses_permissions(self) -> None:
        which = which_with("claude", "codex", "agy")
        for kind in SELECTABLE_AGENTS:
            tool = tools.TOOLS[kind]
            for args in (tool.interactive_args, tool.status_args or (), tool.login_args or ()):
                self.assertFalse(any("dangerously" in a or a in tools.FORBIDDEN_FLAGS for a in args))
            for flag in (*tools.FORBIDDEN_FLAGS, "--dangerously-anything", "--approve-for-me=true"):
                with self.assertRaises(tools.UnsafeArgumentError, msg=flag):
                    tools.build_launch_argv(kind, (flag,), which=which)
        # Opciones normales sí se permiten.
        self.assertEqual(tools.build_launch_argv(AgentKind.CLAUDE_CODE, ("--continue",), which=which),
                         ["/fake/bin/claude", "--continue"])


class AuthTests(unittest.TestCase):
    def test_claude_parser(self) -> None:
        ok = CompletedCommand(0, '{"loggedIn": true, "email": "persona@example.invalid"}')
        self.assertIs(auth.parse_claude_status(ok), AuthState.LOGGED_IN)
        self.assertIs(auth.parse_claude_status(CompletedCommand(1, '{"loggedIn": false}')), AuthState.LOGGED_OUT)
        for bad in ("", "no es json", "[]", '{"loggedIn": "sí"}'):
            self.assertIs(auth.parse_claude_status(CompletedCommand(0, bad)), AuthState.UNKNOWN)

    def test_codex_parser(self) -> None:
        self.assertIs(auth.parse_codex_status(CompletedCommand(0, "Logged in using ChatGPT\n")), AuthState.LOGGED_IN)
        self.assertIs(auth.parse_codex_status(CompletedCommand(1, "Not logged in\n")), AuthState.LOGGED_OUT)
        self.assertIs(auth.parse_codex_status(CompletedCommand(-1, "")), AuthState.UNKNOWN)  # tiempo agotado

    def test_check_uses_official_status_commands(self) -> None:
        calls: list[list[str]] = []

        def run(argv):
            calls.append(argv)
            return CompletedCommand(0, '{"loggedIn": true}' if "claude" in argv[0] else "Logged in using ChatGPT")

        which = which_with("claude", "codex", "agy")
        self.assertIs(auth.check_kind(AgentKind.CLAUDE_CODE, which, run), AuthState.LOGGED_IN)
        self.assertIs(auth.check_kind(AgentKind.CODEX, which, run), AuthState.LOGGED_IN)
        self.assertEqual(calls, [["/fake/bin/claude", "auth", "status", "--json"],
                                 ["/fake/bin/codex", "login", "status"]])

    def test_antigravity_and_missing_tools_run_nothing(self) -> None:
        which = which_with("agy")
        self.assertIs(auth.check_kind(AgentKind.ANTIGRAVITY, which, NoRun()), AuthState.UNKNOWN)
        self.assertIs(auth.check_kind(AgentKind.CLAUDE_CODE, which, NoRun()), AuthState.NOT_INSTALLED)

    def test_run_command_hides_internal_env_and_times_out(self) -> None:
        with mock.patch.dict(os.environ, {"IAW_DATA_DIR": "/secreto"}):
            result = auth.run_command(["/bin/sh", "-c", 'printf "%s" "${IAW_DATA_DIR:-vacío}"'])
        self.assertEqual(result.stdout, "vacío")
        with mock.patch.object(auth, "STATUS_TIMEOUT_SECONDS", 0.2):
            self.assertEqual(auth.run_command(["sleep", "5"]).returncode, -1)
        self.assertEqual(auth.run_command(["/no/existe"]).returncode, -1)


class CatalogTests(unittest.TestCase):
    def test_initial_detection_without_running_anything(self) -> None:
        catalog = AgentCatalog(which=which_with("claude"), run=NoRun(), spawn=lambda fn: fn())
        self.assertTrue(catalog.status(AgentKind.CLAUDE_CODE).installed)
        self.assertIs(catalog.status(AgentKind.CLAUDE_CODE).auth, AuthState.UNKNOWN)
        self.assertIs(catalog.status(AgentKind.CODEX).auth, AuthState.NOT_INSTALLED)
        self.assertEqual(catalog.status(AgentKind.CODEX).summary, "No instalado")

    def test_refresh_is_async_and_keeps_only_the_state(self) -> None:
        pending: list = []
        seen: list[AgentKind] = []
        run = lambda argv: CompletedCommand(0, '{"loggedIn": true, "email": "%s", "orgName": "%s"}' % PRIVATE)
        catalog = AgentCatalog(which=which_with("claude"), run=run, spawn=pending.append)
        catalog.subscribe(seen.append)
        catalog.refresh(AgentKind.CLAUDE_CODE)
        self.assertTrue(catalog.status(AgentKind.CLAUDE_CODE).checking)
        self.assertEqual(catalog.status(AgentKind.CLAUDE_CODE).summary, "Comprobando…")
        pending.pop()()  # el hilo termina
        status = catalog.status(AgentKind.CLAUDE_CODE)
        self.assertFalse(status.checking)
        self.assertIs(status.auth, AuthState.LOGGED_IN)
        self.assertEqual(seen, [AgentKind.CLAUDE_CODE, AgentKind.CLAUDE_CODE])
        snapshot = repr(catalog.__dict__) + repr(catalog.statuses())
        for secret in PRIVATE:
            self.assertNotIn(secret, snapshot, "el catálogo no debe conservar datos de la cuenta")

    def test_ensure_checked_runs_once_and_detect_picks_up_installs(self) -> None:
        installed = {"claude"}
        calls: list[list[str]] = []
        which = lambda name: FAKE_PATHS[name] if name in installed else None
        run = lambda argv: calls.append(argv) or CompletedCommand(0, "Logged in using ChatGPT")
        catalog = AgentCatalog(which=which, run=run, spawn=lambda fn: fn())
        catalog.ensure_checked(AgentKind.CODEX)
        self.assertIs(catalog.status(AgentKind.CODEX).auth, AuthState.NOT_INSTALLED)
        installed.add("codex")
        catalog.detect()
        self.assertTrue(catalog.status(AgentKind.CODEX).installed)
        catalog.ensure_checked(AgentKind.CODEX)
        catalog.ensure_checked(AgentKind.CODEX)
        self.assertEqual(len(calls), 1)
        self.assertIs(catalog.status(AgentKind.CODEX).auth, AuthState.LOGGED_IN)


ROOT_DIR = Path(__file__).resolve().parent.parent
RUN_SH = ROOT_DIR / "scripts" / "run.sh"


class RunScriptPathEnvironmentTests(unittest.TestCase):
    """Verifica que scripts/run.sh prepare un PATH robusto para resolver herramientas de usuario."""

    def _execute_run_sh(
        self,
        home: Path,
        initial_path: str,
        extra_args: tuple[str, ...] = (),
    ) -> tuple[str, str, str]:
        """Ejecuta run.sh interceptando python3 para capturar el entorno final sin iniciar GTK."""
        fake_bin = home / ".fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            'echo "CAPTURED_PATH=$PATH"\n'
            'echo "CAPTURED_SIM=${IAW_SIMULATED_AGENTS:-0}"\n'
            'echo "CAPTURED_ARGS=$*"\n'
        )
        fake_python.chmod(0o755)

        full_path = f"{fake_bin}:{initial_path}" if initial_path else str(fake_bin)
        env = {
            "HOME": str(home),
            "PATH": full_path,
        }
        res = subprocess.run(
            [str(RUN_SH), *extra_args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = dict(
            line.split("=", 1)
            for line in res.stdout.strip().splitlines()
            if "=" in line
        )
        return (
            lines.get("CAPTURED_PATH", ""),
            lines.get("CAPTURED_SIM", "0"),
            lines.get("CAPTURED_ARGS", ""),
        )

    def test_minimal_path_adds_user_local_bin_and_resolves_executable(self) -> None:
        with helpers.temp_dir() as tmp:
            home = Path(tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            fake_agent = local_bin / "custom-agent"
            fake_agent.write_text("#!/bin/sh\necho agent-ok\n")
            fake_agent.chmod(0o755)

            captured_path, _, _ = self._execute_run_sh(home, "/usr/bin:/bin")
            elements = captured_path.split(":")

            self.assertIn(str(local_bin), elements)
            resolved = shutil.which("custom-agent", path=captured_path)
            self.assertEqual(resolved, str(fake_agent))
            self.assertEqual(elements[-2:], ["/usr/bin", "/bin"])

    def test_existing_local_bin_is_not_duplicated(self) -> None:
        with helpers.temp_dir() as tmp:
            home = Path(tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)

            captured_path, _, _ = self._execute_run_sh(home, f"{local_bin}:/usr/bin:/bin")
            elements = captured_path.split(":")
            self.assertEqual(elements.count(str(local_bin)), 1)

    def test_inherited_paths_are_strictly_preserved(self) -> None:
        with helpers.temp_dir() as tmp:
            home = Path(tmp)
            inherited = "/opt/custom/bin:/usr/local/bin:/usr/bin:/bin"
            captured_path, _, _ = self._execute_run_sh(home, inherited)
            elements = captured_path.split(":")
            for p in inherited.split(":"):
                self.assertIn(p, elements)
            idx = [elements.index(p) for p in inherited.split(":")]
            self.assertEqual(idx, sorted(idx))

    def test_no_hardcoded_user_paths_in_run_sh(self) -> None:
        content = RUN_SH.read_text(encoding="utf-8")
        self.assertNotIn("elianmartinez", content)
        self.assertIn('"$HOME/.local/bin"', content)

    def test_simulated_flag_still_works(self) -> None:
        with helpers.temp_dir() as tmp:
            home = Path(tmp)
            _, captured_sim, captured_args = self._execute_run_sh(home, "/usr/bin:/bin", ("--simulated", "foo"))
            self.assertEqual(captured_sim, "1")
            self.assertEqual(captured_args, "-m ia_workspace foo")

    def test_multiple_user_candidates_added_if_they_exist(self) -> None:
        with helpers.temp_dir() as tmp:
            home = Path(tmp)
            (home / ".local" / "bin").mkdir(parents=True)
            (home / ".dotnet" / "tools").mkdir(parents=True)

            captured_path, _, _ = self._execute_run_sh(home, "/usr/bin:/bin")
            elements = captured_path.split(":")

            self.assertIn(str(home / ".local" / "bin"), elements)
            self.assertIn(str(home / ".dotnet" / "tools"), elements)
            self.assertNotIn(str(home / ".opencode" / "bin"), elements)


if __name__ == "__main__":
    unittest.main()
