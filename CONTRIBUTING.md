# Guía de Contribución a IA Workspace

¡Gracias por tu interés en contribuir a IA Workspace! Este es un proyecto de código abierto enfocado en brindar una experiencia de escritorio nativa para Linux respetando los estándares de GNOME y la soberanía de datos del usuario.

## Requisitos de Desarrollo

IA Workspace utiliza las librerías nativas del sistema a través de PyGObject sin requerir paquetes de Python compilados vía `pip`.

### Dependencias del sistema

- **Ubuntu 26.04 LTS / Debian**:
  ```bash
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                   gir1.2-webkit-6.0 gir1.2-vte-3.91 \
                   gstreamer1.0-plugins-bad gstreamer1.0-plugins-good \
                   desktop-file-utils ruff
  ```
- **Arch Linux / CachyOS**:
  ```bash
  sudo pacman -S python-gobject gtk4 libadwaita webkitgtk-6.0 vte4 \
                 gst-plugins-bad gst-plugins-good gst-plugin-pipewire \
                 desktop-file-utils ruff
  ```

## Flujo de Trabajo Local

1. **Ejecución desde el código fuente**:
   ```bash
   scripts/run.sh               # Inicia la aplicación en modo desarrollo (datos en var/)
   scripts/run.sh --simulated   # Inicia con agentes simulados para pruebas rápidas
   ```

2. **Validación completa del proyecto**:
   Antes de proponer cambios, ejecuta la suite de verificación automatizada:
   ```bash
   ./scripts/check.sh
   ```
   Este script verifica:
   - Compilación e integridad de sintaxis en todo el código fuente.
   - Sincronización de iconos del tema hicolor (`scripts/build_icons.py --check`).
   - Pruebas lógicas unitarias (más de 330 pruebas con base de datos y modelos aislados).
   - Pruebas completas de interfaz gráfica y ciclo de vida en un servidor Broadway headless aislado.
   - Auditoría de logs en `var/` para descartar filtraciones accidentales de credenciales o sesiones.

3. **Verificación de estilo y formato**:
   ```bash
   ruff check .
   git diff --check
   ```

## Convenciones de Commits

Seguimos la convención de [Conventional Commits](https://www.conventionalcommits.org/es/v1.0.0/):

- `feat:` Nuevas características o mejoras funcionales.
- `fix:` Corrección de errores o regresiones.
- `docs:` Modificaciones exclusivamente en documentación.
- `test:` Nuevas pruebas o mejoras en las suites de pruebas existentes.
- `perf:` Optimizaciones de rendimiento.
- `chore:` Tareas de mantenimiento, empaquetado o herramientas internas.

## Directrices de Seguridad y Calidad

- **Sin `sudo` en tiempo de ejecución**: Ningún componente de la aplicación ni del instalador debe requerir privilegios de superusuario.
- **Aislamiento de datos**: Los datos de usuario pertenecen a `$XDG_DATA_HOME/ia-workspace`. En desarrollo, siempre deben confinarse a la carpeta `var/` ignorada por Git.
- **Conservación de datos**: Ninguna rutina de desinstalación o migración debe eliminar carpetas de proyectos locales, bases de datos o respaldos del usuario.
