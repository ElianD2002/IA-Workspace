# Arquitectura de IA Workspace

## Capas

```
ui/          GTK 4 + Libadwaita. Solo presenta y delega. Única capa que importa gi.
  window.py      Ventana: barra lateral + pila de pantallas + cierre.
  sidebar.py     Navegación (Alt+1…6), proyectos, integraciones.
  pages/         Inicio, Proyectos, Workspace (uno por proyecto), Agentes, planificadas.
  dialogs/       Agregar proyecto, preferencias, atajos, confirmar salida.
app.py       Adw.Application: ciclo de vida, acciones globales, salida controlada.
context.py   AppContext: composición de dependencias (sin GTK).
services/    Lógica de aplicación: registro de proyectos, proyecto activo y estado
             visual, detección de carpetas, detección de herramientas oficiales.
sessions/    Contratos y gestor de sesiones concurrentes de agentes.
persistence/ SQLite: conexión, migraciones versionadas y repositorios (único SQL).
domain/      Entidades inmutables: Project, AgentKind, WorkspaceState.
```

Las dependencias solo van hacia abajo: `ui → services/sessions → persistence → domain`.
Todo lo que no es `ui/` ni `app.py` se prueba sin display.

## Requisitos y cómo se cumplen

| Requisito | Mecanismo |
|---|---|
| Un único proyecto activo visible | `WorkspaceController.activate()` guarda un solo `active_project_id`; la ventana muestra una sola `WorkspaceView` en su `Gtk.Stack`. |
| Contexto, tareas, conversaciones, terminales y memoria aislados por proyecto | Una `WorkspaceView` por proyecto, con sus widgets, su `WorkspaceState` y sus búferes de salida. `SessionSpec.cwd` es siempre la carpeta del proyecto. Las tablas futuras llevarán `project_id` con `ON DELETE CASCADE`, como `project_ui_state`. |
| Cambiar de proyecto no detiene agentes | Las sesiones viven en `SessionManager`, que pertenece a la aplicación (`AppContext`), no a la vista. Las vistas ocultas siguen recibiendo eventos de sus sesiones. |
| Agentes activos con la ventana minimizada | Minimizar no afecta al bucle principal de GLib ni al gestor. Lo verifica `tests/test_ui.py`. |
| Procesos solo mientras la app esté abierta o minimizada | No hay demonio ni servicio en segundo plano. Al salir del proceso, `SessionManager.shutdown()` finaliza todo. |
| Confirmación y cierre controlado | `close-request`, Ctrl+Q y SIGINT/SIGTERM pasan por `IAWorkspaceApplication.request_quit()`. Con agentes activos se muestra `Adw.AlertDialog`. Al confirmar, se pide cierre ordenado a cada sesión y, tras 10 s de gracia, se fuerza el resto. `quit()` solo se llama cuando no queda ninguna activa. |
| Sin APIs de pago ni credenciales | Los backends lanzarán las CLI oficiales (`claude`, `codex`, `antigravity`) con su autenticación. ChatGPT se abre en el navegador del usuario. El esquema no tiene columnas de secretos (hay una prueba que lo comprueba). |

## Gestor de sesiones (preparado para la fase 2)

```
SessionBackend (Protocol)     uno por AgentKind; is_available(), start(spec, sink) -> SessionHandle
SessionHandle  (Protocol)     send_input(text), stop(force=False)
SessionSink                   state_changed(state, detail, exit_code), output(text)
                              seguro desde cualquier hilo: el gestor reenvía con dispatch
SessionManager                start/stop/send_input/remove/shutdown, sessions(project_id),
                              subscribe(listener) -> eventos started|state|output|removed
```

- **Hilos.** Los backends pueden usar hilos o E/S asíncrona de GLib. Todo cambio de estado se
  aplica en el hilo principal mediante `dispatch` (`GLib.idle_add` en la app y llamada
  directa en las pruebas).
- **Estados.** `STARTING, IDLE, WORKING, WAITING_APPROVAL, STOPPING` son activos;
  `DONE, ERROR, DISCONNECTED, STOPPED` son terminales y definitivos.
- **Desarrollo.** `IAW_SIMULATED_AGENTS=1` registra `SimulatedBackend`, que emite estados
  y salida desde un hilo sin lanzar procesos. La interfaz lo marca como «SIMULADA».

Para incorporar un agente real basta con implementar `SessionBackend` y registrarlo en
`AppContext.create()`. La interfaz no cambia.

## Persistencia

- `persistence/database.py`: migraciones en `MIGRATIONS`, solo se añaden al final y se
  registran en `PRAGMA user_version`. Una base de datos más nueva que el código se rechaza.
- Tablas actuales: `projects`, `project_ui_state` (JSON del `WorkspaceState`) y `settings`.
- El estado visual se acumula en memoria (`mark_dirty`) y se escribe cada 5 s y al salir.

## Correspondencia con el prototipo (`docs/design/`)

| Prototipo | Implementación nativa |
|---|---|
| Barra lateral de 252 px, secciones y proyectos | `Adw.OverlaySplitView` + `Gtk.ListBox.navigation-sidebar` |
| Por debajo de 980 px: barra lateral superpuesta y sin panel ChatGPT | `Adw.Breakpoint` en la ventana y `Adw.BreakpointBin` en cada workspace |
| `.seg` (Actividad/Nombre, Tareas/Memoria) | `Adw.ToggleGroup` |
| `.boxed`, `.row` | `Gtk.ListBox.boxed-list`, `Adw.ActionRow` |
| `.status` | `Adw.StatusPage` (`.compact` en paneles) |
| Paneles redimensionables | `Gtk.Paned`, con tamaños guardados por proyecto |
| Chips CC/CX/AG/GPT, insignias de proyecto, píldoras de estado | `ui/widgets.py` + `ui/style.css` (colores del prototipo) |
| Diálogos | `Adw.Dialog`, `Adw.AlertDialog`, `Adw.PreferencesDialog`, `Adw.ShortcutsDialog` |

### Diferencias deliberadas con el prototipo

- **«Mantener sesiones al cerrar la ventana».** El prototipo lo ofrece, pero contradice el
  requisito de que los procesos solo vivan con la aplicación abierta o minimizada. Se omitió:
  cerrar la ventana equivale a salir, con confirmación.
- **Carpeta `.iaw/` dentro de cada proyecto.** El prototipo guarda allí la memoria. En esta
  fase no se escribe nada en las carpetas de los proyectos. La ubicación de la memoria se
  decidirá en la fase del Cerebro compartido.
- **Funciones futuras.** Búsqueda (Ctrl+K), Biblioteca, vincular conversaciones y
  «Abrir en editor» aparecen deshabilitadas o se omiten, con un aviso.
