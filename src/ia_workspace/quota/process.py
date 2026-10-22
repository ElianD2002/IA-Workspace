"""Subprocesos de consulta de cuotas con un plazo que se cumple siempre.

``subprocess.run(timeout=...)`` solo mata al hijo directo y luego espera a que se cierren
sus tuberías: si la CLI deja procesos nietos con stdout abierto, esa espera no termina.
Y ``readline()`` sobre una tubería bloquea sin límite. Aquí cada proceso arranca en su
propio grupo (``start_new_session``), las tuberías se leen con ``selectors`` contra un plazo
absoluto y, al terminar o vencer el plazo, se finaliza el grupo entero (SIGTERM y, si hace
falta, SIGKILL). Solo se ejecutan argv en lista, nunca a través de un shell.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable

MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # límite de seguridad de lo que se acumula por tubería
_EXIT_GRACE = 0.3  # tras salir el proceso, tiempo para recoger la salida que quede


class BoundedProcess:
    """Proceso con stdin/stdout/stderr en bytes y lecturas limitadas por un plazo absoluto."""

    def __init__(self, argv: list[str], timeout: float) -> None:
        self.argv = argv
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.stdout = b""
        self.stderr = b""
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._selector = selectors.DefaultSelector()
        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._selector.register(self._proc.stdout, selectors.EVENT_READ, "stdout")
        self._selector.register(self._proc.stderr, selectors.EVENT_READ, "stderr")

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def send(self, text: str) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(text.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # el proceso ya terminó: lo que haya respondido se lee igualmente

    def close_stdin(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass

    def _expired(self) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(self.argv, self.timeout)

    def pump(self, until: Callable[[], bool]) -> None:
        """Lee stdout y stderr hasta que ``until()`` se cumpla, las tuberías se cierren o el
        proceso termine. Lanza ``TimeoutExpired`` si vence el plazo antes."""
        exited_at: float | None = None
        while not until():
            now = time.monotonic()
            if now >= self.deadline:
                raise self._expired()
            if not self._selector.get_map():
                return  # EOF en ambas tuberías
            if self._proc.poll() is not None:
                exited_at = exited_at or now
                if now - exited_at > _EXIT_GRACE:
                    return  # salió; un nieto podría mantener la tubería abierta
            for key, _ in self._selector.select(min(0.2, self.deadline - now)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self._selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(self.stdout) < MAX_OUTPUT_BYTES:
                        self.stdout += chunk
                elif len(self.stderr) < MAX_OUTPUT_BYTES:
                    self.stderr += chunk

    def finish(self) -> None:
        """Finaliza el grupo de procesos (si sigue vivo) y libera las tuberías."""
        self.close_stdin()
        for sig, wait in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.5)):
            try:
                os.killpg(self._proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                self._proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        # También quedan nietos del grupo aunque el hijo directo ya haya salido.
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        self._selector.close()
        for pipe in (self._proc.stdout, self._proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


def run_bounded(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Ejecuta ``argv`` sin entrada y devuelve (código, stdout, stderr) dentro del plazo.

    Lanza ``subprocess.TimeoutExpired`` (tras finalizar el grupo) si se agota."""
    proc = BoundedProcess(argv, timeout)
    try:
        proc.close_stdin()
        proc.pump(lambda: False)
        if proc.returncode is None:
            # Las tuberías se cerraron pero el proceso sigue: se le da el resto del plazo.
            while proc.returncode is None:
                if time.monotonic() >= proc.deadline:
                    raise proc._expired()
                time.sleep(0.05)
        code = proc.returncode
    finally:
        proc.finish()
    return (
        code if code is not None else -1,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )
