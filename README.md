# IA Workspace

Aplicación nativa para Linux (GTK 4 + Libadwaita) para gestionar proyectos locales y
coordinar agentes de IA —Claude Code, Codex y Google Antigravity— junto con
conversaciones vinculadas de ChatGPT y memoria independiente por proyecto.

> **Estado: fase 1 — fundación.** Ventana, navegación, registro de proyectos en SQLite,
> estado visual por proyecto y contratos del gestor de sesiones. Todavía no hay
> integraciones reales con los agentes.

## Requisitos (paquetes del sistema)

| Componente  | Mínimo | Probado |
|-------------|--------|---------|
| Python      | 3.12   | 3.14.4  |
| GTK         | la que exija Libadwaita 1.8 | 4.22.2  |
| Libadwaita  | 1.8 (`ShortcutsDialog`, `ToggleGroup`) | 1.9.0   |
| PyGObject   | 3.48   | 3.56.2  |
| SQLite      | 3.35   | 3.46.1  |

En Ubuntu/Debian: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`. No hay dependencias de pip.

## Uso

```bash
scripts/run.sh               # ejecutar desde el código fuente
scripts/run.sh --simulated   # con agentes simulados, para probar sesiones concurrentes
scripts/check.sh             # entorno + compilación + pruebas + prueba de interfaz
scripts/screenshots.sh       # capturas de cada pantalla en var/screenshots/
```

Desde el código fuente, todos los datos (base SQLite, capturas, temporales de pruebas)
se guardan en `var/` dentro del repositorio. Instalada, la aplicación usa
`$XDG_DATA_HOME/ia-workspace/`. `IAW_DATA_DIR` permite elegir otra carpeta.

## Atajos

| Acción | Atajo |
|---|---|
| Inicio · Proyectos · Agentes · Tareas · Cerebro · Historial | Alt+1 … Alt+6 |
| Agregar proyecto | Ctrl+O |
| Nuevo prompt | Ctrl+N |
| Barra lateral | F9 |
| Preferencias · Atajos | Ctrl+, · Ctrl+? |
| Salir | Ctrl+Q |

## Principios

- Cada herramienta usa **su propio inicio de sesión y suscripción**. IA Workspace no pide
  claves API, no guarda credenciales ni tokens y no usa APIs de pago.
- Registrar un proyecto **no modifica su carpeta**: solo se lee (`.git/HEAD`, archivos de stack).
- Los agentes siguen trabajando al **cambiar de proyecto o minimizar**. Al cerrar por
  completo con agentes activos, se pide confirmación y se finalizan de forma ordenada.

Más detalles en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
