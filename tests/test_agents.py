"""Integración con agentes oficiales: lanzamiento, autenticación y catálogo (sin GTK).

Todo con dobles: ninguna prueba ejecuta Claude Code, Codex ni Antigravity reales.
"""

from __future__ import annotations

import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
