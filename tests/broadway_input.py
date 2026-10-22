"""Entrada de ratón real para pruebas en Broadway (``gtk4-broadwayd``).

Broadway recibe la entrada de su cliente web por un websocket (``/socket``, subprotocolo
``broadway``): cada evento es una lista de enteros de 32 bits big-endian
``[tipo, serial, marca de tiempo, …]`` (ver ``sendInput`` en ``broadway.js``). Este cliente
mínimo —solo biblioteca estándar— hace de «navegador»: lee qué superficies existen y envía
movimientos y pulsaciones de botón. GTK los recibe exactamente igual que desde un navegador,
así que pasan por todo el sistema de eventos y controladores (incluidos los de VTE).
Nunca toca el escritorio del usuario: solo el servidor Broadway aislado de las pruebas.
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import threading
import time

EVENT_ENTER = 0
EVENT_POINTER_MOVE = 2
EVENT_BUTTON_PRESS = 3
EVENT_BUTTON_RELEASE = 4
EVENT_KEY_PRESS = 7
EVENT_KEY_RELEASE = 8
EVENT_CONFIGURE_NOTIFY = 11
EVENT_SCREEN_SIZE_CHANGED = 12
EVENT_ROUNDTRIP_NOTIFY = 14
(OP_GRAB_POINTER, OP_UNGRAB_POINTER, OP_NEW_SURFACE, OP_SHOW_SURFACE, OP_HIDE_SURFACE, OP_RAISE_SURFACE,
 OP_LOWER_SURFACE, OP_DESTROY_SURFACE, OP_MOVE_RESIZE, OP_SET_TRANSIENT_FOR, OP_DISCONNECTED,
 OP_SURFACE_UPDATE, OP_SET_SHOW_KEYBOARD, OP_UPLOAD_TEXTURE, OP_RELEASE_TEXTURE, OP_SET_NODES,
 OP_ROUNDTRIP) = range(17)
BUTTON1_MASK = 1 << 8  # GDK_BUTTON1_MASK
SHIFT_MASK = 1 << 0
CONTROL_MASK = 1 << 2
KEY_RETURN = 0xFF0D
FRAME_SECONDS = 1 / 60


def broadway_port() -> int | None:
    display = os.environ.get("BROADWAY_DISPLAY", "")
    if os.environ.get("GDK_BACKEND") != "broadway" or not display.startswith(":"):
        return None
    return 8080 + int(display[1:])


class BroadwayInput:
    def __init__(self, port: int) -> None:
        self.surfaces: dict[int, list[int]] = {}  # id → [x, y, w, h, visible]
        self.serial = 0
        self._lock = threading.Lock()
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (
                "GET /socket HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Protocol: broadway\r\n\r\n"
            ).encode()
        )
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._sock.recv(1)
            if not chunk:
                raise ConnectionError("Broadway cerró la conexión")
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ConnectionError(header.decode(errors="replace"))
        self._sock.settimeout(None)
        self._closed = False
        self._start = time.monotonic()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Como el cliente web al arrancar: tamaño de «pantalla» y escala 1.
        self._send_event([EVENT_SCREEN_SIZE_CHANGED, 1920, 1080, 1])

    # ---- websocket ------------------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                b1, b2 = self._recv_exact(2)
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                payload = self._recv_exact(length)
                if b1 & 0x0F == 2:
                    self._parse(payload)
        except (ConnectionError, OSError):
            pass

    def _parse(self, data: bytes) -> None:
        """Interpreta los comandos del servidor como ``handleCommands`` de ``broadway.js``:
        registra las superficies y responde como el cliente web (``CONFIGURE_NOTIFY`` y
        ``ROUNDTRIP_NOTIFY``, que GTK espera). Ignora texturas y nodos de dibujo."""
        pos = 0
        replies: list[list[int]] = []
        with self._lock:
            while pos + 5 <= len(data):
                op = data[pos]
                self.serial = struct.unpack_from("<I", data, pos + 1)[0]
                pos += 5
                if op == OP_NEW_SURFACE:
                    sid, x, y, w, h = struct.unpack_from("<HhhHH", data, pos)
                    self.surfaces[sid] = [x, y, w, h, 0]
                    replies.append([EVENT_CONFIGURE_NOTIFY, sid, x, y, w, h])
                    pos += 10
                elif op in (OP_SHOW_SURFACE, OP_HIDE_SURFACE, OP_RAISE_SURFACE, OP_LOWER_SURFACE,
                            OP_DESTROY_SURFACE, OP_SET_SHOW_KEYBOARD):
                    (sid,) = struct.unpack_from("<H", data, pos)
                    if sid in self.surfaces and op in (OP_SHOW_SURFACE, OP_HIDE_SURFACE):
                        self.surfaces[sid][4] = 1 if op == OP_SHOW_SURFACE else 0
                    if op == OP_DESTROY_SURFACE:
                        self.surfaces.pop(sid, None)
                    pos += 2
                elif op == OP_SET_TRANSIENT_FOR:
                    pos += 4
                elif op == OP_MOVE_RESIZE:
                    sid, flags = struct.unpack_from("<HB", data, pos)
                    pos += 3
                    surface = self.surfaces.setdefault(sid, [0, 0, 0, 0, 0])
                    if flags & 1:
                        surface[0:2] = struct.unpack_from("<hh", data, pos)
                        pos += 4
                    if flags & 2:
                        surface[2:4] = struct.unpack_from("<HH", data, pos)
                        pos += 4
                    replies.append([EVENT_CONFIGURE_NOTIFY, sid, *surface[:4]])
                elif op == OP_ROUNDTRIP:
                    sid, tag = struct.unpack_from("<HI", data, pos)
                    replies.append([EVENT_ROUNDTRIP_NOTIFY, sid, tag])
                    pos += 6
                elif op == OP_UPLOAD_TEXTURE:
                    (size,) = struct.unpack_from("<I", data, pos + 4)
                    pos += 8 + size
                elif op == OP_RELEASE_TEXTURE:
                    pos += 4
                elif op == OP_SET_NODES:
                    (count,) = struct.unpack_from("<I", data, pos + 2)
                    pos += 6 + 4 * count
                elif op == OP_GRAB_POINTER:
                    pos += 3
                elif op in (OP_UNGRAB_POINTER, OP_DISCONNECTED):
                    pass
                else:
                    break  # comando desconocido: no se puede seguir leyendo este mensaje
        for reply in replies:
            if reply[0] == EVENT_ROUNDTRIP_NOTIFY:
                # Un navegador confirma cada fotograma tras pintarlo (~60 Hz). Confirmarlos al
                # instante haría que GTK pintara sin pausa y acaparase el bucle principal.
                time.sleep(FRAME_SECONDS)
            self._send_event(reply)

    def _send_event(self, args: list[int]) -> None:
        stamp = int((time.monotonic() - self._start) * 1000) + 1
        self._send([args[0], self.serial, stamp, *args[1:]])

    def _send(self, values: list[int]) -> None:
        payload = b"".join(struct.pack(">i", v) for v in values)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes([0x82, 0x80 | len(payload)]) + mask + masked)

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    # ---- ratón ----------------------------------------------------------------------

    def main_surface(self) -> tuple[int, int, int]:
        """(id, x, y) de la superficie visible más grande (la ventana de la aplicación)."""
        with self._lock:
            visible = [(w * h, sid, x, y) for sid, (x, y, w, h, shown) in self.surfaces.items() if shown]
        if not visible:
            raise LookupError("no hay superficies visibles en Broadway")
        _area, sid, x, y = max(visible)
        return sid, x, y

    def _pointer(self, kind: int, surface: tuple[int, int, int], wx: float, wy: float,
                 state: int, extra: list[int]) -> None:
        sid, sx, sy = surface
        stamp = int((time.monotonic() - self._start) * 1000) + 1
        x, y = int(wx), int(wy)
        self._send([kind, self.serial, stamp, sid, sid, sx + x, sy + y, x, y, state, *extra])

    def drag(self, surface: tuple[int, int, int], start: tuple[float, float], end: tuple[float, float],
             steps: int = 12, pump=None) -> None:
        """Pulsa el botón izquierdo en ``start``, arrastra hasta ``end`` y lo suelta.
        Coordenadas relativas a la superficie. ``pump()`` deja procesar a GTK entre pasos."""
        tick = pump or (lambda: time.sleep(0.02))
        self._pointer(EVENT_ENTER, surface, *start, 0, [0])
        self._pointer(EVENT_POINTER_MOVE, surface, *start, 0, [])
        tick()
        self._pointer(EVENT_BUTTON_PRESS, surface, *start, 0, [1])
        tick()
        for i in range(1, steps + 1):
            x = start[0] + (end[0] - start[0]) * i / steps
            y = start[1] + (end[1] - start[1]) * i / steps
            self._pointer(EVENT_POINTER_MOVE, surface, x, y, BUTTON1_MASK, [])
            tick()
        self._pointer(EVENT_BUTTON_RELEASE, surface, *end, BUTTON1_MASK, [1])
        tick()

    def click(self, surface: tuple[int, int, int], at: tuple[float, float], pump=None) -> None:
        self.drag(surface, at, at, steps=0, pump=pump)

    def move(self, surface: tuple[int, int, int], at: tuple[float, float], pump=None) -> None:
        """Mueve el puntero sin pulsar ningún botón."""
        self._pointer(EVENT_POINTER_MOVE, surface, *at, 0, [])
        (pump or (lambda: time.sleep(0.02)))()

    def key(self, keysym: int, state: int = 0, pump=None) -> None:
        """Pulsa y suelta una tecla (``keysym`` X11; en ASCII coincide con el carácter)."""
        tick = pump or (lambda: time.sleep(0.02))
        self._send_event([EVENT_KEY_PRESS, keysym, state])
        tick()
        self._send_event([EVENT_KEY_RELEASE, keysym, state])
        tick()

    def type_text(self, text: str, pump=None) -> None:
        for ch in text:
            self.key(KEY_RETURN if ch == "\n" else ord(ch), pump=pump)
