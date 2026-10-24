# Política de Seguridad

## Versiones con soporte

| Versión | Soportada |
| --- | --- |
| 0.1.0-beta.1 | :white_check_mark: |
| < 0.1.0 | :x: |

## Modelo de Seguridad y Privacidad

IA Workspace está diseñado con un enfoque estricto de mínima confianza y soberanía local:

1. **Sin privilegios elevados**: Toda la aplicación y su instalador funcionan al 100% en el espacio del usuario (`~/.local`). No se requiere ni se recomienda ejecutar nada con `sudo`.
2. **Sin custodia de credenciales de IA**:
   - La aplicación no solicita, no almacena ni transmite API keys o contraseñas.
   - La interacción con Claude Code, OpenAI Codex y Google Antigravity se delega en sus CLI oficiales ya autenticadas en el sistema del usuario.
   - La sesión de ChatGPT se realiza directamente contra los servidores oficiales de OpenAI mediante WebKitGTK 6.0, almacenando las cookies únicamente en el perfil local protegido (`~/.local/share/ia-workspace/webkit`).
3. **Aislamiento web y permisos multimedia**:
   - El motor web restringe la navegación exclusivamente a `chatgpt.com` y rutas auxiliares de inicio de sesión (`auth0.openai.com`). Todo intento de navegación externa se desvía al navegador predeterminado del sistema o se bloquea.
   - El acceso al micrófono está restringido al origen de ChatGPT y requiere aprobación explícita; la cámara y la geolocalización están permanentemente denegadas.
4. **Manejo seguro de capturas y archivos temporales**:
   - Las imágenes adjuntas desde el portapapeles se almacenan en un directorio privado con permisos estrictos `0700` y archivos `0600`.
   - Se limpian automáticamente al cambiar o cerrar la conversación y al cerrar la aplicación.
5. **Integridad de datos locales**:
   - Se implementan bloqueos exclusivos de archivo (`.app.lock` y `.restore.lock`) para prevenir corrupción concurrente en SQLite.
   - Las desinstalaciones o actualizaciones conservan intactos los datos de proyectos, respaldos y el Cerebro Compartido en `$XDG_DATA_HOME/ia-workspace`.

## Notificación de vulnerabilidades

Si descubres una posible vulnerabilidad de seguridad en IA Workspace, por favor repórtala de manera responsable:

- **Canal preferente**: A través de las alertas privadas de seguridad de GitHub (*Security Advisory* en la pestaña de Seguridad del repositorio).
- **Canal alternativo**: Envía un correo con los detalles técnicos a la dirección de contacto del mantenedor indicada en GitHub.

Por favor, incluye:
- Descripción detallada del vector de ataque o vulnerabilidad.
- Pasos precisos para reproducir el problema.
- Entorno (versión de distribución Linux, versión de GTK4, WebKitGTK y Python).

Agradecemos que nos permitas mitigar y publicar una corrección antes de cualquier divulgación pública.
