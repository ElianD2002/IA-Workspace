"""Política de navegación de ChatGPT integrado y garantías de seguridad (sin GTK)."""

from __future__ import annotations

import re
import unittest

from helpers import ROOT

from ia_workspace.services.web_policy import (
    Decision,
    navigation_decision,
    redact,
    resolve_for_view,
    test_base_url,
)


class NavigationPolicyTests(unittest.TestCase):
    def test_https_and_in_page_schemes_are_allowed(self) -> None:
        for uri in ("https://chatgpt.com/c/abc", "https://auth.openai.com/log-in?code=secreto",
                    "https://accounts.google.com/o/oauth2/auth", "about:blank", "about:srcdoc",
                    "data:text/html,<p>x</p>", "blob:https://chatgpt.com/1234"):
            self.assertIs(navigation_decision(uri), Decision.ALLOW, uri)

    def test_everything_else_is_blocked(self) -> None:
        for uri in ("http://chatgpt.com/", "http://127.0.0.1:8000/", "file:///etc/passwd",
                    "javascript:alert(1)", "ftp://example.com/", "chrome://settings", "https:///sin-host",
                    "ws://example.com", "", "no es una url", "http://[::1"):
            self.assertIs(navigation_decision(uri), Decision.BLOCK, uri)

    def test_mail_and_phone_open_outside(self) -> None:
        self.assertIs(navigation_decision("mailto:a@example.com"), Decision.EXTERNAL)
        self.assertIs(navigation_decision("tel:+34600000000"), Decision.EXTERNAL)

    def test_loopback_http_only_when_explicitly_allowed(self) -> None:
        self.assertIs(navigation_decision("http://127.0.0.1:8000/c/x", allow_loopback_http=True), Decision.ALLOW)
        self.assertIs(navigation_decision("http://localhost:8000/", allow_loopback_http=True), Decision.ALLOW)
        self.assertIs(navigation_decision("http://192.168.1.5/", allow_loopback_http=True), Decision.BLOCK)
        self.assertIs(navigation_decision("http://127.0.0.1.evil.example/", allow_loopback_http=True),
                      Decision.BLOCK)


class TestBaseOverrideTests(unittest.TestCase):
    def test_only_loopback_http_is_accepted(self) -> None:
        self.assertEqual(test_base_url({"IAW_CHATGPT_BASE_URL": "http://127.0.0.1:9000/"}), "http://127.0.0.1:9000")
        for bad in ("https://evil.example", "http://evil.example", "http://127.0.0.1.evil.example",
                    "file:///tmp", "", "   "):
            self.assertIsNone(test_base_url({"IAW_CHATGPT_BASE_URL": bad}), bad)
        self.assertIsNone(test_base_url({}))

    def test_resolution(self) -> None:
        url = "https://chatgpt.com/c/abcdefgh-1234"
        self.assertEqual(resolve_for_view(url, None), url)  # producción: siempre la URL canónica
        self.assertEqual(resolve_for_view(url, "http://127.0.0.1:9000"), "http://127.0.0.1:9000/c/abcdefgh-1234")
        self.assertEqual(resolve_for_view("https://otro.example/c/x", "http://127.0.0.1:9000"),
                         "https://otro.example/c/x")


class RedactionTests(unittest.TestCase):
    def test_keeps_only_scheme_and_host(self) -> None:
        self.assertEqual(redact("https://auth.openai.com/callback?code=SECRETO#frag"), "https://auth.openai.com")
        self.assertEqual(redact("https://user:pass@chatgpt.com/c/1"), "https://chatgpt.com")
        self.assertEqual(redact(None), "")
        self.assertEqual(redact("sin esquema"), "(URL no válida)")


class NoContentAccessTests(unittest.TestCase):
    """El código de producción no usa APIs para inyectar o evaluar JavaScript, leer cookies,
    leer el contenido de las páginas ni aceptar certificados inválidos."""

    FORBIDDEN = (
        r"evaluate_javascript", r"run_javascript", r"call_async_javascript_function",
        r"UserContentManager", r"UserScript", r"add_script",
        r"get_cookies", r"get_all_cookies", r"CookieManager\(\)\.get",
        r"get_main_resource", r"get_data\b", r"save_to_file", r"SaveMode", r"save_finish",
        r"allow_tls_certificate_for_host", r"set_tls_errors_policy", r"TLSErrorsPolicy\.IGNORE",
        r"set_user_agent",
    )

    def test_forbidden_apis_are_not_used(self) -> None:
        offenders = []
        for path in (ROOT / "src").rglob("*.py"):
            code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
            for pattern in self.FORBIDDEN:
                if re.search(pattern, code):
                    offenders.append(f"{path.relative_to(ROOT)}: {pattern}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
