# IA Workspace

Aplicación nativa para Linux (GTK 4 + Libadwaita) para gestionar proyectos locales y
coordinar agentes de IA —Claude Code, Codex y Google Antigravity— junto con
conversaciones vinculadas de ChatGPT y memoria independiente por proyecto.

> **Estado: fase 2A — terminales reales.** Ventana, navegación, proyectos en SQLite,
> estado visual por proyecto y terminales VTE por proyecto que siguen ejecutándose en
> segundo plano. Todavía no hay integraciones con Claude Code, Codex ni Antigravity.

## Requisitos (paquetes del sistema)

| Componente  | Mínimo | Probado |
|-------------|--------|---------|
| Python      | 3.12   | 3.14.4  |
| GTK         | la que exija Libadwaita 1.8 | 4.22.2  |
| Libadwaita  | 1.8 (`ShortcutsDialog`, `ToggleGroup`) | 1.9.0   |
| PyGObject   | 3.48   | 3.56.2  |
| SQLite      | 3.35   | 3.46.1  |
| VTE (GTK 4) | 0.76   | 0.84.0  |

En Ubuntu/Debian: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91`. No hay
dependencias de pip. Sin `gir1.2-vte-3.91` la aplicación arranca, pero sin terminales.

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
| Nueva terminal en el proyecto activo | Ctrl+Mayús+T |
| Barra lateral | F9 |
| Preferencias · Atajos | Ctrl+, · Ctrl+? |
| Salir | Ctrl+Q |

## Principios

- Cada herramienta usa **su propio inicio de sesión y suscripción**. IA Workspace no pide
  claves API, no guarda credenciales ni tokens y no usa APIs de pago.
- Registrar un proyecto **no modifica su carpeta**: solo se lee (`.git/HEAD`, archivos de stack).
- Las terminales y agentes siguen trabajando al **cambiar de proyecto o minimizar**. Al
  cerrar por completo con sesiones activas, se pide confirmación y se finalizan de forma
  ordenada, junto con los procesos que hayan iniciado (y solo esos).

Más detalles en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
