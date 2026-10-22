# IA Workspace

Aplicación nativa para Linux (GTK 4 + Libadwaita) para gestionar proyectos locales y
coordinar agentes de IA —Claude Code, Codex y Google Antigravity— junto con
conversaciones vinculadas de ChatGPT y memoria independiente por proyecto.

> **Estado: fase 3A — cerebro compartido en Markdown.** Proyectos en SQLite, workspaces
> independientes, terminales VTE persistentes, sesiones de Claude Code, Codex y Google
> Antigravity mediante sus CLI oficiales, transferencia de prompts y memoria por proyecto
> y global en archivos Markdown, consultable por los agentes bajo demanda. Todavía no hay
> orquestación ni actualización automática de la memoria.

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

### Lanzador e icono en GNOME

`scripts/run.sh` funciona sin instalar nada, y la ventana ya muestra el icono oficial. Para
que IA Workspace aparezca en el menú de aplicaciones, el dock y Alt+Tab con su icono,
instala el lanzador en tu carpeta personal (no usa `sudo` ni toca rutas del sistema):

```bash
scripts/desktop-launcher.sh install     # ~/.local/share/applications + ~/.local/share/icons/hicolor
scripts/desktop-launcher.sh status
scripts/desktop-launcher.sh uninstall   # quita exactamente esos archivos
```

El lanzador ejecuta `scripts/run.sh` de esta copia del repositorio; si la mueves de
carpeta, vuelve a instalarlo. Los tamaños del icono (`data/icons/hicolor/`) se generan a
partir del original aprobado (`data/icons/app-icon.png`, que nunca se modifica) con
`python3 scripts/build_icons.py`; `scripts/check.sh` comprueba que estén al día.

## Agentes

| Herramienta | CLI oficial | Estado de sesión | Inicio de sesión |
|---|---|---|---|
| Claude Code | `claude` | `claude auth status --json` | `claude auth login` |
| Codex | `codex --no-daemon` | `codex login status` | `codex login` |
| Google Antigravity | `agy` | — (lo gestiona la propia CLI) | dentro de la sesión |

En el workspace, el botón **+** de sesiones abre una terminal y su flecha **⌄** permite
iniciar un agente. La CLI se lanza en la carpeta del proyecto, sin opciones que omitan
permisos ni aprobaciones, y con tu suscripción e inicio de sesión. El estado de sesión
solo se consulta al abrir ese menú o con **Preferencias → Integraciones → Comprobar**.

### Transferir un prompt a un agente

En el panel Prompt, elige el agente en **Asignar a** y pulsa **Transferir…**. Un diálogo
de revisión muestra el proyecto, el agente y el prompt completo antes de confirmar nada.
Al confirmar se abre una sesión **nueva** del agente en la carpeta del proyecto, con el
prompt como instrucción inicial mediante el mecanismo oficial de su CLI (nunca se escribe
después en una sesión ya en marcha). Claude Code, Codex y Antigravity admiten los tres
este mecanismo; si un agente no lo tuviera, el botón ofrecería «Copiar y abrir agente» en
su lugar. Cada transferencia queda registrada —proyecto, agente, prompt completo, sesión
y fecha— y aparece en la pestaña **Tareas** del panel derecho, desde donde se puede volver
a la sesión. El registro nunca da por completada una tarea: solo indica si el prompt se
envió o se copió; el progreso real se sigue en el estado de la sesión.

### Conversaciones de ChatGPT

En el panel ChatGPT de cada proyecto, **Vincular conversación** guarda el enlace de una
conversación (`chatgpt.com/c/…`, `chatgpt.com/g/…/c/…`), de un enlace compartido
(`chatgpt.com/share/…`) o de sus equivalentes históricos en `chat.openai.com`. Se abre en
tu navegador con tu sesión habitual. IA Workspace no lee su contenido ni pide credenciales.
Cada proyecto tiene su propia lista: renombrar, copiar el enlace y desvincular están en el
menú **⋮** de cada conversación.

Al pulsar una conversación se abre **dentro de IA Workspace** (WebKitGTK 6.0, paquete
`gir1.2-webkit-6.0`): inicias sesión una vez en el sitio oficial de ChatGPT y la sesión se
guarda en el almacenamiento web propio de la aplicación, compartido por todos los proyectos.
El panel tiene controles para volver a la lista, recargar, abrir en el navegador y una
**vista ampliada**; con la ventana estrecha, un botón en la cabecera la abre. **Preferencias →
Integraciones → Borrar datos web…** cierra esa sesión. Sin WebKitGTK, las conversaciones se
abren en el navegador predeterminado.

Para probar el inicio de sesión de forma aislada, sin tocar los datos de la aplicación:
`python3 scripts/chatgpt_poc.py` (y `--clear` para borrar sus datos).

## Cerebro compartido (memoria en Markdown)

La pantalla **Cerebro compartido** (Alt+5) guarda el conocimiento de cada proyecto —estado
actual, arquitectura, documentación por módulo, decisiones, reglas, próximos pasos y
resúmenes de sesiones— y, aparte, una **memoria global** con tus preferencias generales.
Cada documento es un archivo `.md` en `$XDG_DATA_HOME/ia-workspace/brain/` (desde el
código, `var/brain/`); IA Workspace nunca escribe en tus repositorios ni modifica sus
`AGENTS.md` o `CLAUDE.md`. Cada memoria tiene un `INDEX.md` ligero con una línea por
documento.

Los agentes no reciben la memoria: **Copiar instrucción** da una referencia breve al índice
y a la consulta bajo demanda, que funciona desde la carpeta del proyecto:

```bash
scripts/iaw-brain list                      # una línea por documento
scripts/iaw-brain search "fechas utc"       # resultados con un fragmento
scripts/iaw-brain read decisiones/adr-006-fechas-en-utc.md
scripts/iaw-brain list --global             # memoria global
```

## Atajos

| Acción | Atajo |
|---|---|
| Inicio · Proyectos · Agentes · Tareas · Cerebro · Historial | Alt+1 … Alt+6 |
| Agregar proyecto | Ctrl+O |
| Nuevo prompt | Ctrl+N |
| Nueva terminal en el proyecto activo | Ctrl+Mayús+T |
| Barra lateral | F9 |
| Panel de tareas y memoria (derecho) | Mayús+F9 |
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
