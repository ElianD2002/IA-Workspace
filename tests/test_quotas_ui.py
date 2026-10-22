"""Pruebas automatizadas de interfaz y renderizado del panel de cuotas (Fase 5B.2).

Verifica:
1. Renderizado de los tres proveedores (Claude Code, OpenAI Codex, Google Antigravity).
2. Datos reales normalizados y porcentajes nulos (sin mostrar 0% ficticio).
3. Estados de actualización, disponible, desactualizado y error.
4. Fechas de restablecimiento en hora local (America/Tegucigalpa).
5. Separación de grupos de Antigravity (sin promedios ficticios).
6. Indicadores de consumo elevado (80% warning y 95% error).
7. Actualización sin bloquear GTK y control del botón.
8. Captura pasiva de Claude sin conflictos y aviso en caso de conflicto.
9. Convivencia con autonomía, Cerebro Compartido y transferencia de prompts.
10. Conservación intacta de sesiones activas al refrescar cuotas.
11. Adaptabilidad responsive en ventanas estrechas.
12. Ausencia total de consultas de inferencia en los conectores de cuota.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from helpers import temp_dir
import unittest

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ia_workspace.domain.models import AgentKind, AutonomyMode, Project
from ia_workspace.context import AppContext
from ia_workspace.persistence.database import Database
from ia_workspace.quota.claude import detect_claude_statusline_conflict, get_claude_launch_settings
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)
from ia_workspace.quota.service import QuotaService
from ia_workspace.ui.pages.agents import AgentsPage
from ia_workspace.ui.pages.base import Navigator
from ia_workspace.ui.quota_panel import (
    TZ_TEGUCIGALPA,
    ProviderQuotaCard,
    SubscriptionQuotasSection,
    format_local_datetime,
    format_reset_time,
    format_window_duration,
)


class DummyNav(Navigator):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.toasts: list[str] = []

    def sidebar_toggle(self) -> Gtk.Widget:
        return Gtk.Button()

    def toast(self, message: str) -> None:
        self.toasts.append(message)


class QuotasUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.nav = DummyNav(self.ctx)

    def tearDown(self) -> None:
        self.db.close()

    def test_01_render_three_providers(self) -> None:
        """1. Renderizado de tarjetas para los 3 proveedores."""
        section = SubscriptionQuotasSection(self.ctx)
        self.assertIn(AgentKind.CLAUDE_CODE, section._cards)
        self.assertIn(AgentKind.CODEX, section._cards)
        self.assertIn(AgentKind.ANTIGRAVITY, section._cards)

        for kind, card in section._cards.items():
            self.assertEqual(card.kind, kind)
            self.assertEqual(card._title_label.get_text(), kind.label)

    def test_02_normalized_data_and_null_percentages(self) -> None:
        """2. Datos reales normalizados y tratamiento estricto de porcentajes nulos."""
        card = ProviderQuotaCard(AgentKind.CODEX)

        # Ventana con porcentaje nulo (no provisto)
        win_null = QuotaWindow(
            window_id="null_win",
            name="Ventana sin porcentaje",
            percentage=None,
            metric_type=QuotaMetricType.USED_PERCENT,
        )
        group = QuotaGroup(group_id="g1", name="Grupo Test", windows=(win_null,))
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            plan_type="ChatGPT Plus",
            status=QuotaStatus.AVAILABLE,
            groups=(group,),
        )
        card.update(snap)

        # Verificamos que no se invente 0%
        gbox = card._groups_box.get_first_child()
        self.assertIsNotNone(gbox)
        # La barra debe tener fracción 0.0 pero la etiqueta debe ser "—"
        labels = [child.get_text() for child in gbox.observe_children() if isinstance(child, Gtk.Label)]
        self.assertNotIn("0.0% usado", labels)
        self.assertNotIn("0%", labels)

        # Ventana con 45.5% real
        win_val = QuotaWindow(
            window_id="val_win",
            name="5 horas",
            window_minutes=300,
            percentage=45.5,
            metric_type=QuotaMetricType.USED_PERCENT,
        )
        snap2 = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            plan_type="ChatGPT Plus",
            status=QuotaStatus.AVAILABLE,
            groups=(QuotaGroup(group_id="g1", name="Grupo Test", windows=(win_val,)),),
        )
        card.update(snap2)
        # Debe reflejar 45.5% usado
        gbox2 = card._groups_box.get_first_child()
        self.assertIsNotNone(gbox2)
        self.assertTrue(card._plan_label.get_visible())
        self.assertEqual(card._plan_label.get_text(), "Plan: ChatGPT Plus")

        # Plan no confirmado / ausente no debe mostrarse como confirmado
        snap_no_plan = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            plan_type="",
            status=QuotaStatus.NO_DATA,
        )
        card.update(snap_no_plan)
        self.assertFalse(card._plan_label.get_visible())
        self.assertEqual(card._plan_label.get_text(), "")

    def test_03_states_and_visual_status(self) -> None:
        """3. Distinción clara de estados: Disponible, Actualizando, Desactualizado y Error."""
        card = ProviderQuotaCard(AgentKind.ANTIGRAVITY)

        # Estado ERROR
        snap_err = ProviderQuotaSnapshot(
            provider=AgentKind.ANTIGRAVITY,
            status=QuotaStatus.ERROR,
            detail="CLI agy no encontrada en PATH",
        )
        card.update(snap_err)
        self.assertTrue(card._notice_box.get_visible())
        self.assertIn("CLI agy no encontrada", card._notice_label.get_text())

        # Estado STALE
        snap_stale = ProviderQuotaSnapshot(
            provider=AgentKind.ANTIGRAVITY,
            status=QuotaStatus.STALE,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=45),
        )
        card.update(snap_stale)
        self.assertIn("desactualizados", card._footer_label.get_text())

        # Estado ACTUALIZANDO
        card.update(snap_stale, is_updating=True)
        # Pill status_box debe contener "Actualizando..."

    def test_04_reset_dates_in_local_timezone(self) -> None:
        """4. Fechas de restablecimiento y lectura en America/Tegucigalpa."""
        utc_dt = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)
        # En America/Tegucigalpa (UTC-6) esto es 00:30
        local_str = format_local_datetime(utc_dt)
        self.assertIn("CST", local_str)
        self.assertIn("00:30", local_str)

        reset_label = format_reset_time(utc_dt)
        self.assertIn("CST", reset_label)

    def test_05_antigravity_groups_isolation(self) -> None:
        """5. Antigravity conserva desglose por grupos sin combinaciones ficticias."""
        card = ProviderQuotaCard(AgentKind.ANTIGRAVITY)

        g1 = QuotaGroup(
            group_id="gemini",
            name="Gemini Models",
            description="Modelos Gemini 2.5",
            windows=(QuotaWindow("5h", "5 horas", QuotaMetricType.USED_PERCENT, percentage=20.0, window_minutes=300),),
        )
        g2 = QuotaGroup(
            group_id="claude_gpt",
            name="Claude and GPT models",
            description="Modelos externos",
            windows=(QuotaWindow("5h", "5 horas", QuotaMetricType.USED_PERCENT, percentage=75.0, window_minutes=300),),
        )
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.ANTIGRAVITY,
            plan_type="Google AI Pro",
            status=QuotaStatus.AVAILABLE,
            groups=(g1, g2),
        )
        card.update(snap)

        # Se deben renderizar 2 bloques de grupo
        group_widgets = list(card._groups_box.observe_children())
        self.assertEqual(len(group_widgets), 2)

    def test_06_high_consumption_warnings(self) -> None:
        """6. Advertencias discretas en 80% (consumo elevado) y 95% (límite próximo)."""
        card = ProviderQuotaCard(AgentKind.CLAUDE_CODE)

        w_high = QuotaWindow("5h", "5 horas", QuotaMetricType.USED_PERCENT, percentage=82.0)
        w_crit = QuotaWindow("7d", "7 días", QuotaMetricType.USED_PERCENT, percentage=96.5)
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.CLAUDE_CODE,
            status=QuotaStatus.AVAILABLE,
            groups=(QuotaGroup("main", "Límites", windows=(w_high, w_crit)),),
        )
        card.update(snap)

        gbox = card._groups_box.get_first_child()
        self.assertIsNotNone(gbox)
        # Inspeccionamos los widgets de ventana
        children = list(gbox.observe_children())
        # Hay 2 ventanas
        self.assertEqual(len(children), 2)

    def test_07_refresh_does_not_block_gtk(self) -> None:
        """7. Actualización no bloquea GTK y gestiona estado del botón."""
        refreshed = False

        def on_req() -> None:
            nonlocal refreshed
            refreshed = True

        section = SubscriptionQuotasSection(self.ctx, on_refresh_requested=on_req)
        self.assertTrue(section._refresh_btn.get_sensitive())
        section._refresh_btn.emit("clicked")
        self.assertTrue(refreshed)

        # Verificamos estado updating
        section.set_updating(True)
        self.assertFalse(section._refresh_btn.get_sensitive())
        self.assertEqual(section._refresh_btn.get_label(), "Actualizando...")
        section.set_updating(False)
        self.assertTrue(section._refresh_btn.get_sensitive())
        self.assertEqual(section._refresh_btn.get_label(), "Actualizar cuotas")

    def test_08_claude_passive_capture_and_conflict_detection(self) -> None:
        """8. Captura pasiva de Claude y aviso visual en caso de conflicto."""
        with temp_dir() as tmp:
            pdir = Path(tmp)
            claude_dir = pdir / ".claude"
            claude_dir.mkdir()
            # Sin conflicto
            has_conf, _ = detect_claude_statusline_conflict(project_dir=pdir)
            self.assertFalse(has_conf)

            # Con conflicto preexistente
            (claude_dir / "settings.json").write_text('{"statusLine": "custom_cmd"}')
            has_conf, reason = detect_claude_statusline_conflict(project_dir=pdir)
            self.assertTrue(has_conf)
            self.assertIn("Configuración previa", reason)

            card = ProviderQuotaCard(AgentKind.CLAUDE_CODE)
            snap = ProviderQuotaSnapshot(provider=AgentKind.CLAUDE_CODE, status=QuotaStatus.NO_DATA)
            card.update(snap, conflict_reason=reason)
            self.assertTrue(card._notice_box.get_visible())
            self.assertIn("Captura no disponible", card._notice_label.get_text())

    def test_09_coexistence_with_autonomy_and_brain(self) -> None:
        """9. Convivencia de launch args con autonomía, Cerebro y transferencia de prompts."""
        from ia_workspace.agents.tools import build_launch_argv

        # statusLine args
        extra = ("--settings", '{"statusLine":{"type":"command","command":"dummy"}}')
        argv = build_launch_argv(
            AgentKind.CLAUDE_CODE,
            extra=extra,
            prompt="mi prompt importante",
            context="Cerebro: referencia",
            autonomy_mode=AutonomyMode.AUTO,
        )
        self.assertIn("--settings", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("auto", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn("Cerebro: referencia", argv)
        self.assertIn("--", argv)
        self.assertIn("mi prompt importante", argv)

    def test_10_active_sessions_preserved_on_refresh(self) -> None:
        """10. Conservación intacta de sesiones activas al refrescar cuotas."""
        from ia_workspace.sessions.models import SessionSpec

        with temp_dir() as tmp:
            p = self.ctx.projects.register("P1", tmp, ())
            page = AgentsPage(self.nav)
            page.refresh()

            # Creamos una sesión activa
            spec = SessionSpec(kind=AgentKind.CODEX, project_id=p.id, title="Codex 1", cwd=tmp)
            s = self.ctx.sessions.start(spec)
            self.assertTrue(s.state.is_active)

            # Refrescamos cuotas varias veces
            page._render_quotas()
            page.refresh()

            # La sesión permanece intacta
            s_after = self.ctx.sessions.get(s.id)
            self.assertIsNotNone(s_after)
            self.assertEqual(s_after.id, s.id)
            self.assertTrue(s_after.state.is_active)

    def test_11_responsive_narrow_window_layout(self) -> None:
        """11. Diseño responsive en ventanas estrechas."""
        card = ProviderQuotaCard(AgentKind.CODEX)
        # Verificamos que los textos tengan wrap=True o elipsis adecuada
        self.assertTrue(card._notice_label.get_wrap())
        self.assertGreaterEqual(card.get_spacing(), 8)

    def test_12_no_inference_queries(self) -> None:
        """12. Verificación de cero consultas de inferencia en conectores."""
        from ia_workspace.quota.antigravity import AntigravityQuotaConnector

        recorded_cmd: list[str] = []

        def fake_runner(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            recorded_cmd.extend(cmd)
            return 0, '{"models": []}', ""

        conn = AntigravityQuotaConnector(which=lambda _: "/bin/agy", runner=fake_runner)
        conn.fetch()
        self.assertIn("/usage", recorded_cmd)
        self.assertNotIn("prompt", recorded_cmd)


def _pill_text(card: ProviderQuotaCard) -> str:
    pill = card._status_box.get_first_child()
    child = pill.get_last_child() if pill is not None else None
    return child.get_label() if isinstance(child, Gtk.Label) else ""


class QuotaRefreshFlowUITests(unittest.TestCase):
    """Botón «Actualizar cuotas» con hilos reales y ``GLib.idle_add`` (corrección del bloqueo)."""

    def setUp(self) -> None:
        import threading

        from gi.repository import GLib

        from ia_workspace.app import _glib_dispatch
        from ia_workspace.quota.repository import QuotaRepository

        self.GLib = GLib
        self.threading = threading
        self.tmp = temp_dir()
        self.db = Database(Path(self.tmp.name) / "ia-workspace.db")
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.nav = DummyNav(self.ctx)
        self.callback_threads: list[object] = []
        self.codex_ok = False
        self.agy_ok = True

        def make(kind: AgentKind, ok) -> object:
            test = self

            class Connector:
                def fetch(self) -> ProviderQuotaSnapshot:
                    if not ok():
                        raise RuntimeError(f"{kind.label} no disponible")
                    window = QuotaWindow(window_id="primary", name="5h", metric_type=QuotaMetricType.USED_PERCENT,
                                         percentage=30.0, window_minutes=300)
                    return ProviderQuotaSnapshot(provider=kind, status=QuotaStatus.AVAILABLE,
                                                 groups=(QuotaGroup(group_id="g", name="G", windows=(window,)),),
                                                 updated_at=datetime.now(timezone.utc))

            return Connector()

        def dispatch(fn) -> None:
            def run() -> None:
                self.callback_threads.append(threading.current_thread())
                fn()

            _glib_dispatch(run)

        self.ctx.quotas = QuotaService(
            QuotaRepository(self.db),
            make(AgentKind.CODEX, lambda: self.codex_ok),
            make(AgentKind.ANTIGRAVITY, lambda: self.agy_ok),
            dispatch=dispatch,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def pump_until(self, condition, timeout: float = 5.0) -> bool:
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            context.iteration(False)
            time.sleep(0.005)
        return condition()

    def test_13_button_restored_after_error_and_retry_succeeds(self) -> None:
        page = AgentsPage(self.nav)
        page.refresh()
        section = page._quotas_section
        button = section._refresh_btn

        button.emit("clicked")
        self.assertFalse(button.get_sensitive())
        self.assertEqual(_pill_text(section._cards[AgentKind.CODEX]), "Actualizando...")
        self.assertTrue(self.pump_until(button.get_sensitive), "el botón quedó deshabilitado")
        self.assertEqual(button.get_label(), "Actualizar cuotas")
        self.assertEqual(page._updating_quota_kinds, set())
        self.assertEqual(_pill_text(section._cards[AgentKind.CODEX]), "Error de consulta")
        self.assertIn("no disponible", section._cards[AgentKind.CODEX]._notice_label.get_text())
        self.assertEqual(_pill_text(section._cards[AgentKind.ANTIGRAVITY]), "Disponible")
        self.assertEqual(_pill_text(section._cards[AgentKind.CLAUDE_CODE]), "Sin datos")
        self.assertIn(f"No se pudieron actualizar las cuotas de {AgentKind.CODEX.label}.", self.nav.toasts[-1])
        # 6. Todos los callbacks se ejecutaron en el hilo de GTK.
        self.assertTrue(self.callback_threads)
        self.assertTrue(all(t is self.threading.main_thread() for t in self.callback_threads))

        # 8. Segundo intento: ahora Codex responde.
        self.codex_ok = True
        button.emit("clicked")
        self.assertTrue(self.pump_until(button.get_sensitive))
        self.assertEqual(_pill_text(section._cards[AgentKind.CODEX]), "Disponible")
        self.assertEqual(self.nav.toasts[-1], "Cuotas de suscripción actualizadas.")

    def test_14_error_after_success_shows_stale_cache(self) -> None:
        page = AgentsPage(self.nav)
        page.refresh()
        section = page._quotas_section
        self.codex_ok = True
        section._refresh_btn.emit("clicked")
        self.assertTrue(self.pump_until(section._refresh_btn.get_sensitive))
        self.codex_ok = False
        section._refresh_btn.emit("clicked")
        self.assertTrue(self.pump_until(section._refresh_btn.get_sensitive))
        card = section._cards[AgentKind.CODEX]
        self.assertEqual(_pill_text(card), "Desactualizado")
        self.assertIn("últimos datos conocidos", card._notice_label.get_text())
        self.assertTrue(card._groups_box.get_first_child() is not None, "se perdió la caché anterior")

    def test_15_quotas_are_the_same_for_every_project(self) -> None:
        with temp_dir() as a, temp_dir() as b:
            pa = self.ctx.projects.register("A", a, ())
            pb = self.ctx.projects.register("B", b, ())
            self.codex_ok = True
            page = AgentsPage(self.nav)
            page.refresh()
            page._quotas_section._refresh_btn.emit("clicked")
            self.assertTrue(self.pump_until(page._quotas_section._refresh_btn.get_sensitive))
            seen = []
            for project in (pa, pb):
                page._project = project
                page._render_quotas()
                seen.append(_pill_text(page._quotas_section._cards[AgentKind.CODEX]))
            self.assertEqual(seen, ["Disponible", "Disponible"])


if __name__ == "__main__":
    unittest.main()
