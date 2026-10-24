# IA Workspace

Aplicación nativa para Linux (GTK 4 + Libadwaita) diseñada para gestionar proyectos locales y coordinar agentes de inteligencia artificial —Claude Code, OpenAI Codex y Google Antigravity— junto con sesiones integradas de ChatGPT, memoria documental compartida por proyecto y monitor unificado de consumo de suscripciones.

> **Versión: v0.1.0-beta.1**
> Probado y verificado en **Ubuntu 26.04 LTS** (GNOME 46+ / Wayland). Compatible con **Arch Linux / CachyOS** mediante paquetes oficiales de la distribución.

---

## Características Principales

- **Diseño Nativo GNOME / Libadwaita**: Construido en Python con GTK 4, Libadwaita y tipografía del sistema, respetando las guías de interfaz GNOME HIG, soporte para modo claro/oscuro y diálogo de atajos.
- **Terminales VTE Persistentes**: Pestañas de consola embebidas para interactuar con tus proyectos y agentes, con paletas de color Catppuccin (Mocha, Macchiato, Frappe, Latte) y copiado automático al seleccionar con el ratón.
- **Orquestación de Agentes de IA**:
  - Lanzamiento y supervisión de **Claude Code**, **OpenAI Codex** y **Google Antigravity** mediante sus CLI oficiales.
  - Detección de sesión y estado de autenticación sin almacenar contraseñas ni API keys en la aplicación.
  - Transferencia asistida de prompts con previsualización completa y registro histórico de tareas.
- **Cerebro Compartido en Markdown**:
  - Memoria documental local por proyecto y global (`~/.local/share/ia-workspace/cerebro`).
  - Herramienta de línea de comandos `iaw-brain` para inyección de contexto bajo demanda.
  - Flujo de revisión interactiva de propuestas para aprobar o rechazar modificaciones sugeridas por los agentes antes de aplicarlas.
- **ChatGPT Integrado (WebKitGTK 6.0)**:
  - Navegador web integrado confinado estrictamente al dominio oficial `chatgpt.com`.
  - Soporte de **dictado por voz** nativo mediante PipeWire y GStreamer (`uritranscodebin`).
  - Capacidad para **adjuntar capturas PNG del portapapeles** instantáneamente en las conversaciones, sin necesidad de guardar archivos temporales a mano.
  - Selector de archivos tradicional mediante el diálogo nativo `Gtk.FileDialog`.
- **Monitor de Consumo y Cuotas**: Visualización unificada del estado de cuotas de tus suscripciones de IA configuradas para planificar y optimizar sesiones de trabajo.
- **Respaldos Seguros y Recuperación Offline**:
  - Creación de copias de seguridad locales comprimidas (`.tar.gz`) con verificación de integridad.
  - Asistente gráfico independiente de recuperación (`iaw-restore-gui`) y herramienta CLI (`iaw-restore`) para restauraciones seguras en frío.
  - Mecanismo de exclusividad mutua mediante bloqueos de archivo para evitar la ejecución concurrente o la corrupción de datos.

---

## Requisitos del Sistema

| Componente | Requisito mínimo | Probado |
|---|---|---|
| **Distribución** | Linux con entorno de escritorio moderno (GNOME recomendado) | Ubuntu 26.04 LTS |
| **Python** | >= 3.12 | 3.14.4 |
| **GTK** | 4.12+ | 4.22.4 |
| **Libadwaita** | 1.8+ (`ShortcutsDialog`, `ToggleGroup`) | 1.9.1 |
| **PyGObject** | 3.48+ | 3.56.2 |
| **WebKitGTK** | 6.0 | 2.46+ |
| **VTE (GTK 4)** | 0.76+ | 0.84.0 |
| **GStreamer** | 1.20+ (con plugins good y bad para dictado) | 1.24+ |

### Dependencias por Distribución

IA Workspace utiliza las librerías nativas del sistema operativo; **no requiere paquetes de Python compilados con pip**.

#### Ubuntu 26.04 LTS / Debian
```bash
sudo apt update
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-webkit-6.0 gir1.2-vte-3.91 \
                 gstreamer1.0-plugins-bad gstreamer1.0-plugins-good \
                 gstreamer1.0-pipewire desktop-file-utils
```

#### Arch Linux / CachyOS (Compatibilidad preliminar)
```bash
sudo pacman -S python-gobject gtk4 libadwaita webkitgtk-6.0 vte4 \
               gst-plugins-bad gst-plugins-good gst-plugin-pipewire \
               desktop-file-utils
```

---

## Instalación y Uso

### 1. Instalación Nativa de Usuario (Recomendada)

El instalador funciona **100% en el espacio de usuario** (en `~/.local`), sin requerir permisos de `sudo`:

```bash
# Desde el archivo descomprimido de la release o el repositorio:
scripts/instalador-local.sh install
```

Este proceso:
1. Valida las librerías del sistema y los bloqueos exclusivos.
2. Copia la aplicación a `~/.local/share/ia-workspace-app`.
3. Instala los binarios `ia-workspace`, `iaw-restore-gui` y utilidades auxiliares en `~/.local/bin`.
4. Configura los lanzadores `.desktop` oficiales en `~/.local/share/applications`.
5. Instala los iconos oficiales en todos los tamaños estándar hicolor (16x16 hasta 512x512).

### 2. Comprobar el Estado de la Instalación

```bash
scripts/instalador-local.sh status
```

### 3. Actualización

Para actualizar a una versión superior, simplemente descarga la nueva versión y ejecuta:

```bash
scripts/instalador-local.sh install
```
El instalador cuenta con sistema de *rollback* automático y conserva íntegros tus datos, proyectos, base de datos SQLite y sesiones web en `~/.local/share/ia-workspace`.

### 4. Desinstalación

```bash
scripts/instalador-local.sh uninstall
```
Elimina limpiamente los binarios, lanzadores e iconos registrados en el manifiesto de instalación. **Tus datos de proyectos, configuraciones y respaldos se conservan protegidos** en tu carpeta de usuario.

---

## Ejecución desde el Código Fuente

Si prefieres ejecutar la aplicación directamente en modo desarrollo sin instalarla:

```bash
scripts/run.sh               # Ejecución normal (datos de prueba aislados en var/)
scripts/run.sh --simulated   # Con agentes simulados (ideal para entornos de prueba)
./scripts/check.sh           # Batería completa de pruebas (compilación, lógica e interfaz en Broadway)
```

---

## Limitaciones Conocidas de la Versión Beta

- **Dictado por voz en ChatGPT**: Requiere que la librería GStreamer contenga el elemento `uritranscodebin` (proporcionado por `gstreamer1.0-plugins-bad` en Ubuntu o `gst-plugins-bad` en Arch/CachyOS).
- **Inicio de sesión en ChatGPT**: El primer inicio de sesión debe realizarse manualmente en la vista web integrada de ChatGPT. Las cookies de sesión se conservan en el sandbox de WebKitGTK del usuario.
- **Compatibilidad con distribuciones**: La suite de pruebas de interfaz automatizada se ejecuta nativamente sobre Ubuntu 26.04. En distribuciones basadas en Arch o CachyOS, se agradece el reporte de incidencias o comportamientos específicos en el rastreador de problemas.

---

## Licencia

Este proyecto está distribuido bajo la licencia **GNU General Public License v3.0 o posterior (GPL-3.0-or-later)**. Consulta el archivo [LICENSE](LICENSE) para más detalles.

Los temas de color Catppuccin incluidos se rigen bajo su respectiva licencia MIT. Los iconos del tema hicolor y recursos gráficos de la aplicación son creaciones originales de IA Workspace distribuidas conjuntamente bajo los términos de la licencia GPLv3+.
