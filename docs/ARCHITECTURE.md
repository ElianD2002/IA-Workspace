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
agents/      Agentes oficiales (sin GTK): tools.py (comandos oficiales y barrera contra
             opciones que omiten permisos), auth.py (estado de sesión), catalog.py.
terminal/    Terminales reales: process.py (sin GTK: shell, entorno, procesos de la sesión
             vía /proc), backend.py (SessionBackend con VTE) y theme.py (colores).
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

## Terminales (fase 2A)

```
SessionManager (aplicación) ──► VteTerminalHandle ──► Vte.Terminal + proceso (PTY, setsid)
        ▲                               │ widget()
        │ eventos started|state|removed ▼
WorkspaceView (una por proyecto) ── _output_stack: aloja el widget, nunca lo posee
```

- **Propiedad.** `VteTerminalBackend` (`AgentKind.SHELL`) implementa `SessionBackend`.
  El handle crea y posee el `Vte.Terminal`. La vista solo lo aloja en su pila de salida
  mediante el contrato `ViewableHandle.widget()`.
- **Continuidad.** VTE sigue leyendo el PTY con el widget oculto o la ventana minimizada,
  así que los procesos no se bloquean. Cambiar de pestaña, de proyecto o de sección solo
  cambia qué hijo de la pila se muestra; nada se destruye ni se relanza. `SessionManager`
  conserva el handle de una sesión terminada (para ver su salida final) hasta `remove()`,
  que llama a `dispose()`.
- **Directorio y entorno.** `SessionSpec.cwd` es la raíz del proyecto (se comprueba que
  exista). El entorno es el del usuario más `IAW_PROJECT_ID`, sin las variables `IAW_*`
  internas. El shell es `$SHELL` o el de `/etc/passwd` (`IAW_TERMINAL_SHELL` solo para
  pruebas).
- **Procesos gestionados.** VTE lanza cada terminal en una sesión POSIX nueva, en la que
  el PID del shell es el id de sesión. `terminal/process.py` localiza los procesos de esa
  sesión y sus descendientes leyendo `/proc`, de modo que nunca señaliza procesos ajenos,
  ni el propio IA Workspace, ni el PID 1.
- **Detener.** SIGHUP a toda la sesión (como cerrar un emulador de terminal) y SIGKILL si
  algo sigue vivo tras 5 s. Si el shell termina y quedan procesos suyos (`nohup`, `disown`,
  trabajos que ignoran señales), reciben SIGTERM y, 1 s después, SIGKILL. Solo entonces se
  informa `STOPPED`/`DONE`. Tras informar el final, nunca se vuelve a señalizar ese PID,
  porque el sistema podría haberlo reutilizado.
- **Salida de la aplicación.** Sigue el flujo de la fase 1 (confirmación y
  `SessionManager.shutdown`: cierre ordenado y, tras 10 s, forzado). Como red de seguridad,
  `do_shutdown` envía SIGKILL a cualquier proceso gestionado que quedara.
- **Acciones.** «+» o Ctrl+Mayús+T abre una terminal nueva. Las pestañas seleccionan la
  sesión. «Detener» pide confirmación. «Cerrar» la pide solo si hay procesos en curso
  además del shell; cerrar una sesión terminada no pregunta.

### Limitaciones conocidas

- Un proceso que llame a `setsid()` por su cuenta (un demonio) abandona la sesión. Se
  sigue localizando por ascendencia mientras su proceso padre exista.
- Si IA Workspace termina de forma abrupta (SIGKILL o caída), no hay cierre ordenado. El
  kernel cierra el PTY y los shells reciben SIGHUP, pero los procesos que lo ignoren
  pueden sobrevivir.
- Los atajos globales de la aplicación (Ctrl+Q, Ctrl+N, Ctrl+O) tienen prioridad sobre la
  terminal enfocada.
- Los colores de la terminal siguen el tema claro/oscuro de Libadwaita. Un tema GTK
  global en `~/.config/gtk-4.0/gtk.css` puede forzar otros colores en el resto de la
  interfaz.

## Agentes oficiales (fase 2B.1)

```
menú «+ ⌄» del workspace ─► WorkspaceView.start_agent(kind)
                               │  (si la herramienta indica que falta sesión: diálogo «Iniciar sesión»)
                               ▼
SessionManager.start(SessionSpec(kind, cwd=carpeta del proyecto, mode=interactive|login))
                               ▼
VteTerminalBackend(kind, argv_builder) ─► agents.tools.build_launch_argv / build_login_argv
```

- **Reutilización.** Cada agente es un `VteTerminalBackend` con otro constructor de comando.
  Hereda la continuidad, el aislamiento por proyecto, las acciones de detener y cerrar, y
  el cierre controlado de la fase 2A sin código nuevo de procesos.
- **Comandos.** Solo se usan los modos interactivos oficiales (`claude`, `codex`, `agy`) y
  sus comandos de estado e inicio de sesión, verificados con `--help` en este equipo.
  `build_launch_argv` rechaza cualquier opción que omita permisos, aprobaciones o el
  sandbox (`--dangerously-*`, `--approve-for-me`, `--permission-mode`…), aunque llegue
  por error.
- **Autenticación.** `AgentCatalog` consulta en un hilo, y solo bajo demanda (al abrir el
  menú o en Preferencias), los comandos oficiales de estado. Solo guarda en memoria el
  `AuthState`: la salida (que incluye correo y organización) se descarta. El inicio de
  sesión es una sesión VTE más que ejecuta el flujo oficial; el usuario lo completa en
  la terminal o en su navegador. IA Workspace no lee archivos de credenciales.
- **Herramientas ausentes.** El menú las muestra como «No instalado» y deshabilitadas.
  Por API, la sesión termina en `ERROR` con un mensaje claro.
- **Modo simulado.** Con `IAW_SIMULATED_AGENTS=1` no se registran las CLI reales. Las
  pruebas de agentes usan CLI falsas y un `PATH` que excluye las reales.

### Limitaciones de la fase 2B.1

- Las sesiones de agentes figuran como «En ejecución» mientras su proceso vive. Aún no se
  detecta si están trabajando o esperando aprobación.
- Antigravity (`agy`) no ofrece comando de estado ni de inicio de sesión: su autenticación
  se verifica al abrir la sesión. Si solo estuviera la aplicación gráfica, IA Workspace no
  la controla. En este equipo no hay ninguna instalada (`antigravity` no está en el PATH
  ni hay un archivo `.desktop`).
- El selector «Asignar a» y el botón «Transferir…» del panel Prompt siguen reservados para
  la transferencia de prompts (fase posterior).

## Transferencia de prompts a agentes (fase 2B.2)

```
«Transferir…» ─► WorkspaceView.request_transfer()
                    │  (TransferPromptDialog: proyecto, agente, prompt completo, confirmar)
                    ▼ on_confirm(method)
              WorkspaceView._execute_transfer(kind, prompt, method)
                    │
                    ▼
    SessionManager.start(SessionSpec(kind, cwd=carpeta del proyecto,
                                     initial_prompt=prompt si method == "direct"))
                    │
                    ▼
    VteTerminalBackend ─► agents.tools.build_launch_argv(kind, prompt=…)
                    │
                    ▼
    ctx.prompt_transfers.record(proyecto, agente, prompt, sesión, method, status)
```

- **Siempre una sesión nueva.** La transferencia nunca escribe en una sesión ya en marcha
  (no hay forma fiable de saber si está lista para recibir texto): construye un
  `SessionSpec` con `initial_prompt` y dependerá del mecanismo oficial de la CLI, igual
  que el resto de sesiones de la fase 2B.1. Reutiliza por completo `SessionManager`,
  `VteTerminalBackend` y el ciclo de vida (continuidad entre proyectos, detener, cerrar,
  cierre controlado): no hay código nuevo de procesos.
- **Mecanismo oficial por agente** (verificado con `--help` y, lanzando cada CLI en un
  terminal real hasta su primera pantalla de confirmación, sin aceptarla ni enviar nada
  al modelo — ver el docstring de `agents/tools.py` para el detalle completo):
  - Claude Code y Codex: `[prompt]` como argumento posicional, precedido de un `--`
    literal (la forma estándar de Commander.js y de clap para que un prompt que empiece
    por «-» nunca se confunda con una opción).
  - Antigravity: `--prompt-interactive <texto>`.
  - El texto viaja como un único elemento de `argv` (`Vte.Terminal.spawn_async` usa
    `execve`, no un intérprete de órdenes): ningún carácter especial, comilla, barra
    invertida o salto de línea necesita escaparse ni puede alterar el comando.
  - `build_launch_argv` valida las *opciones* antes de añadir el prompt, así que el
    contenido del prompt nunca puede disparar `UnsafeArgumentError` por casualidad (por
    ejemplo, al mencionar la palabra «dangerously» en una frase normal).
- **Alternativa manual.** Si una herramienta no tuviera mecanismo directo
  (`AgentTool.supports_direct_prompt` falso), el diálogo ofrece «Copiar y abrir agente»:
  copia el prompt al portapapeles y abre una sesión nueva sin prompt incrustado, para
  pegarlo a mano. Hoy los tres agentes admiten transferencia directa; el mecanismo manual
  se conserva para herramientas futuras y se prueba forzándolo con una `AgentTool` sin
  mecanismo de prompt.
- **Registro, sin atribuir éxito.** La tabla `prompt_transfers` (migración 3) guarda
  proyecto, agente, prompt completo, el id de sesión (mientras exista en esta ejecución),
  el método (`direct`/`manual`) y un `status` que solo describe si el prompt se envió o se
  copió (`sent`/`copied`/`error`) — nunca si el agente completó la tarea. El progreso real
  se sigue por separado, en el estado de la sesión (`sessions.models.SessionState`).
- **Interfaz.** La pestaña «Tareas» del panel derecho (antes un estado vacío fijo) lista
  las transferencias del proyecto activo, con el estado en vivo de su sesión cuando sigue
  existiendo esta ejecución; al pulsar una fila se vuelve a esa sesión. Es la misma pestaña
  del prototipo, sin pantallas nuevas.
- **Errores sin perder el prompt.** Si la sesión termina en `ERROR` (herramienta ausente,
  opción no permitida…), el borrador del editor no se vacía y la transferencia queda
  registrada con `status="error"` y el detalle del fallo.

## Icono oficial e integración con GNOME

- **Origen único.** `data/icons/app-icon.png` (1254×1254, RGBA, aprobado) nunca se modifica.
  `scripts/build_icons.py` genera `data/icons/hicolor/<N>x<N>/apps/<APP_ID>.png` (16 a
  512 px) reduciéndolo directamente con interpolación de alta calidad, sin recortes ni
  retoques; `--check` (en `scripts/check.sh`) falla si algún tamaño está desactualizado.
- **Una sola identidad.** El nombre del icono, el archivo `.desktop`, `StartupWMClass` y el
  id de `Adw.Application` son el mismo: `io.github.elianmartinez.IAWorkspace`. En GNOME
  Wayland, GTK publica ese id como `app_id` de la ventana y la Shell la asocia a su `.desktop`
  (icono en el dock y Alt+Tab). En X11, `main()` fija el `prgname` al mismo valor para que
  `WM_CLASS` coincida.
- **Desde el código fuente.** `IAWorkspaceApplication._setup_icon()` añade `data/icons` a la
  búsqueda del tema de iconos y fija `Gtk.Window.set_default_icon_name(APP_ID)`; el diálogo
  «Acerca de» usa el mismo nombre.
- **Lanzador local reversible.** `scripts/desktop-launcher.sh install|uninstall|status`
  copia el `.desktop` (con `Exec`/`TryExec` apuntando a `scripts/run.sh` de esta copia) y
  los iconos a `$XDG_DATA_HOME`; nunca usa rutas del sistema, solo actualiza la caché de
  iconos si ya existía y `uninstall` borra exactamente los archivos con el nombre del id.
- **Limitación.** A 16 y 24 px el diseño (muy detallado) pierde el detalle interior; no se
  redibuja para conservar el original aprobado.

## ChatGPT integrado (WebKitGTK 6.0)

```
fila de conversación ─► WorkspaceView.open_conversation(link_id)   (solo enlaces del proyecto)
                           │
                           ▼
              ChatBrowser (uno por proyecto) ─► WebKit.WebView
                           │                          │
                           │                network_session(data_dir/webkit)  (compartida,
                           │                persistente: cookies.sqlite de WebKitGTK)
                           ▼
              panel ChatGPT  ⇄  ChatExpandedDialog (vista ampliada; misma WebView, trasladada)
```

- **Sitio oficial, sin trucos.** Se carga `https://chatgpt.com/...` con el User-Agent estándar
  de WebKitGTK; el usuario inicia sesión con el flujo oficial. IA Workspace no inyecta ni
  evalúa JavaScript, no lee cookies ni el contenido de las páginas y no copia nada a SQLite;
  `tests/test_web_policy.py` comprueba que el código no usa esas APIs.
- **Almacenamiento.** `NetworkSession` persistente en `<datos>/webkit/{data,cache}` (con el
  código fuente, `var/webkit/`), compartida por todos los proyectos: la autenticación es
  común, pero cada proyecto tiene su propia `WebView` y solo abre sus conversaciones.
  «Borrar datos web» usa `WebsiteDataManager.clear` (sin leer los datos).
- **Política** (`services/web_policy.py`): solo HTTPS (y `about:`/`data:`/`blob:` para el
  contenido propio de la página); `file:`, `javascript:`, `http:` y otros esquemas se
  bloquean; `mailto:`/`tel:` y las ventanas nuevas se abren en el sistema. Se deniegan todos
  los permisos (micrófono, notificaciones…), se bloquean las descargas y los errores TLS no
  se aceptan nunca. Sin herramientas de desarrollo ni ventanas emergentes automáticas.
- **Ciclo de vida.** Al salir se terminan los procesos web y se sueltan las sesiones; una
  prueba en subproceso verifica que ningún proceso de WebKit sobrevive a la aplicación.
- **Pruebas.** `IAW_CHATGPT_BASE_URL` (solo `http` en loopback; se ignora cualquier otra cosa)
  sirve las conversaciones desde un servidor simulado; `scripts/check.sh` la fija por
  defecto a un puerto cerrado para que ninguna prueba cargue el sitio real.

### Limitaciones conocidas

- El inicio de sesión real solo puede confirmarlo el usuario. Los flujos por redirección
  (correo, Google, Microsoft, Apple desde «Iniciar sesión») deberían funcionar; un flujo que
  dependa de una ventana emergente (p. ej. el aviso «Continuar con Google» flotante) puede
  no completarse, porque las ventanas nuevas se abren en el navegador externo, que no
  comparte la sesión. En ese caso hay que usar «Iniciar sesión» en la propia página.
- Voz, cámara, notificaciones y descargas no funcionan en la vista integrada (permisos
  denegados por diseño): para eso, «Abrir en el navegador».
- Proveedores de identidad pueden rechazar navegadores integrados en el futuro; el
  navegador externo sigue disponible en todas las conversaciones.

## Corrección unificada tras las primeras pruebas reales

- **Panel derecho plegable.** Hay un botón en el extremo derecho de la cabecera del
  workspace (simétrico al de la barra izquierda) y el atajo Mayús+F9 (`win.toggle-context`).
  Usa el campo existente `WorkspaceState.show_context`, guardado por proyecto en
  `project_ui_state`. Ocultar solo cambia la visibilidad: la pestaña y el contenido se
  conservan, y `Gtk.Paned` entrega todo el ancho al centro.
- **Conversaciones de ChatGPT.** `services/chatgpt_links.py` (validación pura),
  `services/chat_links.py` (servicio) y la tabla `chat_links` (migración 2). Solo se
  guardan el enlace canónico, su tipo (`private`/`shared`), el id de la conversación y un
  título. Hay unicidad por `(proyecto, tipo, id)` y `ON DELETE CASCADE` con el proyecto.
  Los cambios notifican solo al proyecto afectado. Se abren con `Gtk.UriLauncher` en el
  navegador del usuario, sin vista web integrada ni sincronización del historial.
- **Codex.** La instalación de este equipo es el servidor de aplicaciones de un plugin
  (`~/.codex/plugins/.plugin-appserver/codex`), sin el paquete local que exige el daemon
  compartido. Por eso `codex` sale con código 1 («no complete local package»).
  IA Workspace lo lanza con la opción oficial `--no-daemon`, que funciona con cualquier
  instalación y mantiene el servidor dentro de la sesión de la terminal, así que el
  cierre controlado también lo finaliza. Para usar el daemon compartido fuera de IA
  Workspace haría falta el instalador independiente de Codex; no se instala nada
  automáticamente.
- **Estados de finalización.** `terminal.process.classify_exit` asigna el estado a partir
  del código de salida real:
  - `DONE` («Finalizado correctamente»): código 0;
  - `FAILED` («Finalizado con error»): código distinto de 0 o una señal que no envió el
    usuario;
  - `STOPPED` («Detenido por el usuario»): detenido por el usuario o al salir de IA
    Workspace.

  `ERROR` queda para «no se pudo iniciar». Mientras el proceso vive, la sesión figura
  «En ejecución»: nunca se deduce que una tarea terminó bien por ver su prompt. La salida
  de la terminal se conserva, y la barra de la sesión muestra el detalle final.

## Persistencia

- `persistence/database.py`: migraciones en `MIGRATIONS`, solo se añaden al final y se
  registran en `PRAGMA user_version`. Una base de datos más nueva que el código se rechaza.
- Tablas actuales: `projects`, `project_ui_state` (JSON del `WorkspaceState`), `settings`,
  `chat_links` (desde la migración 2) y `prompt_transfers` (desde la migración 3).
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
