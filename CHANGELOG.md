# Changelog
Todas las modificaciones notables de este proyecto se documentan en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y este proyecto se adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0-beta.1] - 2026-10-23

### Añadido
- **Interfaz nativa GNOME/Linux**: Construida en Python con GTK 4 y Libadwaita, respetando las directrices de diseño GNOME Human Interface Guidelines (HIG).
- **Espacios de trabajo multiproyecto**: Gestión organizada de proyectos locales con estados visuales, tareas vinculadas y aislamiento de datos.
- **Terminales VTE persistentes**: Pestañas de consola embebidas con soporte para la paleta Catppuccin (Mocha, Macchiato, Frappe, Latte) y copia automática al seleccionar con ratón.
- **Orquestación de agentes de IA**:
  - Lanzamiento y vinculación de Claude Code, OpenAI Codex y Google Antigravity mediante sus CLI oficiales.
  - Transferencia segura de prompts con diálogo de previsualización y registro histórico inmutable de tareas.
  - Detección del estado de sesión y autenticación sin almacenar credenciales en la aplicación.
- **Cerebro Compartido en Markdown**:
  - Memoria contextual documental por proyecto y global.
  - Puente CLI (`iaw-brain`) para consultar memorias bajo demanda.
  - Sistema de revisión interactiva de propuestas generadas por los agentes antes de consolidarlas en disco.
- **ChatGPT integrado en WebKitGTK 6.0**:
  - Navegador web integrado con aislamiento de red estricto (restringido a `chatgpt.com`).
  - Soporte para dictado por voz nativo mediante GStreamer y PipeWire.
  - Adjunto ágil de capturas de pantalla PNG tomadas al portapapeles (`Gdk.Texture` -> archivo temporal seguro con permisos `0700`/`0600` y ciclo de vida controlado).
  - Selector de archivos nativo tradicional mediante `Gtk.FileDialog`.
- **Panel de consumo de cuotas de IA**: Monitor unificado de actividad, ventanas de facturación y límites de suscripción para los modelos configurados.
- **Copias de seguridad y recuperación offline**:
  - Servicio de respaldos locales comprimidos (`.tar.gz`) con validación de integridad SHA-256.
  - Asistente gráfico de recuperación (`iaw-restore-gui`) y CLI independiente (`iaw-restore`) para restauraciones seguras en frío.
  - Bloqueo exclusivo de procesos (`.app.lock` y `.restore.lock`) para prevenir corrupción concurrente.
- **Instalador nativo en espacio de usuario**:
  - `scripts/instalador-local.sh`: Instalación, actualización y desinstalación sin privilegios de `sudo` en `~/.local`.
  - Integración completa con GNOME Shell (iconos hicolor escalables de 16x16 a 512x512 y lanzadores `.desktop` validados).
  - Protección estricta de datos del usuario: nunca elimina ni altera la base de datos ni los respaldos en `~/.local/share/ia-workspace`.

### Limitaciones de la Beta
- Probado y verificado en Ubuntu 26.04 LTS (GNOME / Wayland).
- Compatible con Arch Linux / CachyOS mediante paquetes oficiales del sistema (pendiente de retroalimentación de la comunidad).
- La sesión web de ChatGPT requiere autenticación manual en la primera ejecución y almacenamiento persistente en el sandbox local.
