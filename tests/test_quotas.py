"""Pruebas unitarias de límites de cuota y consumo de suscripciones de IA (Fase 5B.1).

Verifica conectores, normalización, persistencia SQLite, ausencia de consumo de
inferencia, y robustez frente a errores y configuraciones previas del usuario.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest

from helpers import temp_dir

from ia_workspace.domain.models import AgentKind, AutonomyMode, NewProject
from ia_workspace.persistence.database import SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.quota import (
    AntigravityQuotaConnector,
    CodexQuotaConnector,
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaRepository,
    QuotaService,
    QuotaStatus,
    QuotaWindow,
    detect_claude_statusline_conflict,
    get_claude_launch_settings,
    parse_claude_statusline_payload,
)
from ia_workspace.sessions.models import SessionSpec

# Fixtures sanitizados basados en la estructura real observada
FIXTURE_CODEX_DUAL_WINDOWS = {
    "planType": "plus",
    "rateLimitsByLimitId": {
        "chatgpt_general": {
            "primary": {"usedPercent": 80, "windowDurationMins": 180, "resetsAt": 1792660000}
        },
        "codex": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1792664346},
            "secondary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1793110605},
        },
    },
}

FIXTURE_CODEX_SINGLE_WINDOW = {
    "planType": "pro",
    "rateLimitsByLimitId": {
        "codex": {
            "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1792664346},
        }
    },
}

FIXTURE_ANTIGRAVITY_GROUPS = {
    "conversation_id": "",
    "status": "SUCCESS",
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    },
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.7688,
                            "reset_time": "2026-10-26T17:03:26Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.9052,
                            "reset_time": "2026-10-22T09:47:52Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "description": "Claude Opus, Claude Sonnet, GPT-OSS",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 1.0,
                            "reset_time": "2026-10-29T05:20:17Z",
                        },
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "description": "Argon quota is separate.",
                    "buckets": [
                        {
                            "id": "gemini-argon-wtu-weekly",
                            "name": "Gemini 4 Argon Weekly Limit",
                            "window": "weekly",
                            "remaining_fraction": 0.0019,
                            "reset_time": "2026-10-25T02:18:21Z",
                        }
                    ],
                },
            ]
        },
    },
}


class QuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.repo = QuotaRepository(self.db)

    # 1. Cuotas de Codex con una o varias ventanas
    def test_codex_single_and_multiple_windows(self) -> None:
        # Caso varias ventanas
        def runner_dual(argv: list[str], requests: list[str], timeout: float):
            resp1 = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
            resp2 = json.dumps({"jsonrpc": "2.0", "id": 2, "result": FIXTURE_CODEX_DUAL_WINDOWS})
            return [resp1, resp2], ""

        connector = CodexQuotaConnector(which=lambda _: "/bin/codex", runner=runner_dual)
        snap = connector.fetch()
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE)
        self.assertEqual(snap.plan_type, "plus")
        self.assertEqual(len(snap.groups), 1)
        codex_group = snap.groups[0]
        self.assertEqual(codex_group.group_id, "codex")
        self.assertEqual(len(codex_group.windows), 2)

        win_primary = codex_group.windows[0]
        self.assertEqual(win_primary.window_id, "primary")
        self.assertEqual(win_primary.used_percentage, 0.0)
        self.assertEqual(win_primary.window_minutes, 300)

        win_secondary = codex_group.windows[1]
        self.assertEqual(win_secondary.window_id, "secondary")
        self.assertEqual(win_secondary.used_percentage, 25.0)
        self.assertEqual(win_secondary.window_minutes, 10080)

        # Caso una sola ventana
        def runner_single(argv: list[str], requests: list[str], timeout: float):
            resp1 = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
            resp2 = json.dumps({"jsonrpc": "2.0", "id": 2, "result": FIXTURE_CODEX_SINGLE_WINDOW})
            return [resp1, resp2], ""

        connector_single = CodexQuotaConnector(which=lambda _: "/bin/codex", runner=runner_single)
        snap_single = connector_single.fetch()
        self.assertEqual(snap_single.status, QuotaStatus.AVAILABLE)
        self.assertEqual(snap_single.plan_type, "pro")
        self.assertEqual(len(snap_single.groups[0].windows), 1)
        self.assertEqual(snap_single.groups[0].windows[0].used_percentage, 42.0)

    # 2. Cuotas separadas por modelo en Antigravity
    def test_antigravity_separate_model_groups(self) -> None:
        def runner_agy(argv: list[str], timeout: float):
            return 0, json.dumps(FIXTURE_ANTIGRAVITY_GROUPS), ""

        connector = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_agy)
        snap = connector.fetch()
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE)
        self.assertEqual(snap.provider, AgentKind.ANTIGRAVITY)
        self.assertEqual(len(snap.groups), 3)

        group_names = [g.name for g in snap.groups]
        self.assertIn("Gemini Models", group_names)
        self.assertIn("Claude and GPT models", group_names)
        self.assertIn("Gemini 4 Argon", group_names)

        gemini_group = next(g for g in snap.groups if g.name == "Gemini Models")
        self.assertEqual(len(gemini_group.windows), 2)
        weekly = next(w for w in gemini_group.windows if w.window_id == "gemini-weekly")
        self.assertAlmostEqual(weekly.remaining_percentage or 0.0, 76.88, places=2)
        self.assertEqual(weekly.metric_type, QuotaMetricType.REMAINING_PERCENT)

        argon_group = next(g for g in snap.groups if g.name == "Gemini 4 Argon")
        self.assertEqual(len(argon_group.windows), 1)
        argon_win = argon_group.windows[0]
        self.assertAlmostEqual(argon_win.remaining_percentage or 0.0, 0.19, places=2)

    # 3. Datos pasivos de Claude
    def test_claude_passive_statusline_ingestion(self) -> None:
        payload = json.dumps(
            {
                "session_id": "test-session",
                "model": {"display_name": "Claude 3.7 Sonnet"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 14.5, "resets_at": 1729580400},
                    "seven_day": {"used_percentage": 32.0, "resets_at": 1730185200},
                },
            }
        )
        snap = parse_claude_statusline_payload(payload)
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE)
        self.assertEqual(snap.provider, AgentKind.CLAUDE_CODE)
        self.assertEqual(len(snap.groups[0].windows), 2)
        self.assertEqual(snap.groups[0].windows[0].used_percentage, 14.5)
        self.assertEqual(snap.groups[0].windows[1].used_percentage, 32.0)

        # Guardar en repositorio
        self.repo.save_snapshot(snap)
        recovered = self.repo.get_snapshot(AgentKind.CLAUDE_CODE)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.status, QuotaStatus.AVAILABLE)
        self.assertEqual(recovered.groups[0].windows[0].used_percentage, 14.5)

    # 4. Campos ausentes y porcentajes nulos (NUNCA convertir a 0%)
    def test_missing_fields_and_null_percentages(self) -> None:
        # En Claude antes de que haya respuesta en la sesión
        empty_payload = json.dumps({"session_id": "fresh-session"})
        snap_empty = parse_claude_statusline_payload(empty_payload)
        self.assertEqual(snap_empty.status, QuotaStatus.NO_DATA)
        self.assertEqual(len(snap_empty.groups), 0)

        # En Antigravity si un bucket no trae remaining_fraction
        corrupt_agy = {
            "status": "SUCCESS",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Special",
                            "buckets": [
                                {
                                    "id": "special-quota",
                                    "name": "Límite desconocido",
                                    "remaining_fraction": None,
                                }
                            ],
                        }
                    ]
                },
            },
        }

        def runner_null(argv: list[str], timeout: float):
            return 0, json.dumps(corrupt_agy), ""

        connector = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_null)
        snap = connector.fetch()
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE)
        window = snap.groups[0].windows[0]
        self.assertIsNone(window.percentage)
        self.assertIsNone(window.used_percentage)
        self.assertIsNone(window.remaining_percentage)
        self.assertNotEqual(window.percentage, 0.0)

    # 5. Conversiones de porcentaje restante a utilizado
    def test_remaining_to_used_percentage_conversions(self) -> None:
        # QuotaWindow con REMAINING_PERCENT (ej. 75% restante -> 25% usado)
        win_rem = QuotaWindow(
            window_id="test-rem",
            name="Restante",
            metric_type=QuotaMetricType.REMAINING_PERCENT,
            percentage=75.0,
        )
        self.assertEqual(win_rem.remaining_percentage, 75.0)
        self.assertEqual(win_rem.used_percentage, 25.0)

        # QuotaWindow con USED_PERCENT (ej. 30% usado -> 70% restante)
        win_used = QuotaWindow(
            window_id="test-used",
            name="Usado",
            metric_type=QuotaMetricType.USED_PERCENT,
            percentage=30.0,
        )
        self.assertEqual(win_used.used_percentage, 30.0)
        self.assertEqual(win_used.remaining_percentage, 70.0)

        # QuotaWindow nulo
        win_none = QuotaWindow(
            window_id="test-none",
            name="Nulo",
            metric_type=QuotaMetricType.USED_PERCENT,
            percentage=None,
        )
        self.assertIsNone(win_none.used_percentage)
        self.assertIsNone(win_none.remaining_percentage)

    # 6. Timestamps UTC
    def test_utc_timestamps_and_resets(self) -> None:
        reset_dt = datetime(2026, 10, 26, 17, 3, 26, tzinfo=timezone.utc)
        win = QuotaWindow(
            window_id="w-utc",
            name="UTC Test",
            metric_type=QuotaMetricType.USED_PERCENT,
            percentage=10.0,
            resets_at=reset_dt,
        )
        group = QuotaGroup(group_id="g-utc", name="Group UTC", windows=(win,))
        now_dt = datetime(2026, 10, 26, 15, 3, 26, tzinfo=timezone.utc)
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            status=QuotaStatus.AVAILABLE,
            groups=(group,),
            updated_at=now_dt,
        )

        self.assertEqual(win.seconds_until_reset(now=now_dt), 7200.0)
        self.repo.save_snapshot(snap)

        rec = self.repo.get_snapshot(AgentKind.CODEX)
        assert rec is not None
        rec_win = rec.groups[0].windows[0]
        self.assertIsNotNone(rec_win.resets_at)
        assert rec_win.resets_at is not None
        self.assertEqual(rec_win.resets_at.tzinfo, timezone.utc)
        self.assertEqual(rec_win.resets_at.isoformat(), "2026-10-26T17:03:26+00:00")

    # 7. Errores, timeouts y respuestas inválidas
    def test_errors_timeouts_and_invalid_responses(self) -> None:
        # Timeout en Codex
        def runner_timeout(argv: list[str], requests: list[str], timeout: float):
            raise subprocess.TimeoutExpired(argv, timeout)

        conn_codex = CodexQuotaConnector(which=lambda _: "/bin/codex", runner=runner_timeout)
        snap_codex = conn_codex.fetch()
        self.assertEqual(snap_codex.status, QuotaStatus.ERROR)
        self.assertIn("Tiempo de espera agotado", snap_codex.detail)

        # JSON corrupto en Antigravity
        def runner_corrupt(argv: list[str], timeout: float):
            return 0, "{ corrupt json ...", ""

        conn_agy = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_corrupt)
        snap_agy = conn_agy.fetch()
        self.assertEqual(snap_agy.status, QuotaStatus.ERROR)
        self.assertIn("Respuesta JSON no válida", snap_agy.detail)

        # Binario no encontrado
        conn_missing = CodexQuotaConnector(which=lambda _: None)
        snap_missing = conn_missing.fetch()
        self.assertEqual(snap_missing.status, QuotaStatus.ERROR)
        self.assertIn("no está instalada", snap_missing.detail)

    # 8. Persistencia sin credenciales
    def test_persistence_without_credentials(self) -> None:
        self.assertEqual(self.db.version, SCHEMA_VERSION)
        tables = [r[0] for r in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("subscription_quotas", tables)

        # Ninguna columna con nombres prohibidos
        forbidden = ("token", "secret", "password", "api_key", "apikey", "credential", "cookie")
        for column in self.db.conn.execute("PRAGMA table_info(subscription_quotas)"):
            col_name = column[1].lower()
            self.assertFalse(any(f in col_name for f in forbidden), f"Columna prohibida: {col_name}")

        # Guardar snapshot completo y verificar que no hay payloads sensibles
        win = QuotaWindow("p", "Principal", QuotaMetricType.USED_PERCENT, 10.0, 300)
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            plan_type="plus",
            status=QuotaStatus.AVAILABLE,
            groups=(QuotaGroup("codex", "Codex", "", (win,)),),
            updated_at=datetime.now(timezone.utc),
        )
        self.repo.save_snapshot(snap)

        raw_row = self.db.conn.execute("SELECT * FROM subscription_quotas WHERE provider_id = 'codex'").fetchone()
        self.assertIsNotNone(raw_row)
        self.assertEqual(raw_row["plan_type"], "plus")
        self.assertEqual(raw_row["percentage"], 10.0)

    # 9. Compatibilidad de sesiones existentes y preservación de statusLine
    def test_compatibility_existing_sessions(self) -> None:
        with temp_dir() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            home.mkdir(parents=True)
            project.mkdir(parents=True)

            # Caso 1: Sin configuración previa de statusLine -> Se inyecta sin problema
            has_conflict, _ = detect_claude_statusline_conflict(home_dir=home, project_dir=project)
            self.assertFalse(has_conflict)
            settings_args = get_claude_launch_settings("/fake/script", home_dir=home, project_dir=project)
            self.assertEqual(len(settings_args), 2)
            self.assertEqual(settings_args[0], "--settings")
            parsed_settings = json.loads(settings_args[1])
            self.assertEqual(parsed_settings["statusLine"]["command"], "/fake/script")

            # Caso 2: El usuario ya tiene statusLine en ~/.claude/settings.json
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / "settings.json").write_text(
                json.dumps({"statusLine": {"type": "command", "command": "my_custom_status.sh"}}),
                encoding="utf-8",
            )

            has_conflict2, reason = detect_claude_statusline_conflict(home_dir=home, project_dir=project)
            self.assertTrue(has_conflict2)
            self.assertIn("Configuración previa detectada", reason)

            # La inyección se desactiva por completo para respetar la del usuario
            settings_args2 = get_claude_launch_settings("/fake/script", home_dir=home, project_dir=project)
            self.assertEqual(settings_args2, ())

    # 10. Ausencia de consumos de inferencia en pruebas
    def test_zero_inference_tokens_enforcement(self) -> None:
        # Si un reporte administrativo de agy reportara tokens o turnos, debe registrarse
        data_with_tokens = json.loads(json.dumps(FIXTURE_ANTIGRAVITY_GROUPS))
        data_with_tokens["num_turns"] = 1
        data_with_tokens["usage"]["total_tokens"] = 150

        def runner_tokens(argv: list[str], timeout: float):
            return 0, json.dumps(data_with_tokens), ""

        conn = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_tokens)
        snap = conn.fetch()
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("Fallo seguro: se detectó consumo de inferencia", snap.detail)

        # En operación normal con fixture real verificado, 0 tokens y 0 turnos
        def runner_zero(argv: list[str], timeout: float):
            return 0, json.dumps(FIXTURE_ANTIGRAVITY_GROUPS), ""

        conn_zero = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_zero)
        snap_zero = conn_zero.fetch()
        self.assertEqual(snap_zero.status, QuotaStatus.AVAILABLE)
        self.assertEqual(snap_zero.detail, "")

    def test_antigravity_nested_response_structure(self) -> None:
        wrapped_fixture = {
            "status": "SUCCESS",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
            "response": FIXTURE_ANTIGRAVITY_GROUPS["command"],
        }

        def runner_wrapped(argv: list[str], timeout: float):
            return 0, json.dumps(wrapped_fixture), ""

        conn = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_wrapped)
        snap = conn.fetch()
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE)
        self.assertEqual(len(snap.groups), 3)

    def test_antigravity_status_failure_fails_safe(self) -> None:
        err_fixture = {
            "status": "ERROR",
            "error": "El comando '/usage' no está soportado en esta versión.",
            "num_turns": 0,
            "usage": {"total_tokens": 0},
        }

        def runner_err(argv: list[str], timeout: float):
            return 0, json.dumps(err_fixture), ""

        conn = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=runner_err)
        snap = conn.fetch()
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("Fallo seguro: agy devolvió estado no exitoso", snap.detail)

    def test_sqlite_migration_7_to_8_preserves_data(self) -> None:
        from ia_workspace.persistence.database import MIGRATIONS

        with temp_dir() as tmp:
            db_path = Path(tmp) / "mig.db"
            conn = sqlite3.connect(db_path)
            for migration in MIGRATIONS[:7]:
                for statement in migration:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 7")
            conn.execute(
                "INSERT INTO projects (name, path, color_index, agents, created_at) "
                "VALUES ('Migrado', '/migrado', 0, '[]', '2026-10-01T10:00:00+00:00')"
            )
            conn.commit()
            conn.close()

            db = Database(db_path)
            self.assertEqual(db.version, 8)
            row = db.conn.execute("SELECT name FROM projects WHERE path = '/migrado'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Migrado")
            quota_count = db.conn.execute("SELECT COUNT(*) FROM subscription_quotas").fetchone()[0]
            self.assertEqual(quota_count, 0)
            db.close()


class QuotaServiceTests(unittest.TestCase):
    def test_service_cooldown_and_dispatch(self) -> None:
        db = Database(":memory:")
        repo = QuotaRepository(db)

        calls = {"codex": 0, "agy": 0}

        def mock_runner_codex(argv: list[str], requests: list[str], timeout: float):
            calls["codex"] += 1
            return [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "result": FIXTURE_CODEX_DUAL_WINDOWS}),
            ], ""

        def mock_runner_agy(argv: list[str], timeout: float):
            calls["agy"] += 1
            return 0, json.dumps(FIXTURE_ANTIGRAVITY_GROUPS), ""

        codex_conn = CodexQuotaConnector(which=lambda _: "/bin/codex", runner=mock_runner_codex)
        agy_conn = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=mock_runner_agy)

        service = QuotaService(
            repo=repo,
            codex_connector=codex_conn,
            antigravity_connector=agy_conn,
            cooldown_seconds=60.0,
            stale_threshold_seconds=10.0,
        )

        # Refresco sincrónico 1
        snap_cx1 = service.refresh_codex_sync()
        self.assertEqual(snap_cx1.status, QuotaStatus.AVAILABLE)
        self.assertEqual(calls["codex"], 1)

        # Refresco sincrónico 2 dentro del cooldown (sin force) -> no incrementa llamada
        snap_cx2 = service.refresh_codex_sync(force=False)
        self.assertEqual(snap_cx2.status, QuotaStatus.AVAILABLE)
        self.assertEqual(calls["codex"], 1)

        # Refresco con force=True -> realiza consulta
        snap_cx3 = service.refresh_codex_sync(force=True)
        self.assertEqual(snap_cx3.status, QuotaStatus.AVAILABLE)
        self.assertEqual(calls["codex"], 2)

        snap_ag = service.refresh_antigravity_sync()
        self.assertEqual(snap_ag.status, QuotaStatus.AVAILABLE)
        self.assertEqual(calls["agy"], 1)

        # Lectura mediante get_all_snapshots
        all_snaps = service.get_all_snapshots()
        self.assertEqual(all_snaps[AgentKind.CODEX].status, QuotaStatus.AVAILABLE)
        self.assertEqual(all_snaps[AgentKind.ANTIGRAVITY].status, QuotaStatus.AVAILABLE)
        self.assertEqual(all_snaps[AgentKind.CLAUDE_CODE].status, QuotaStatus.NO_DATA)
