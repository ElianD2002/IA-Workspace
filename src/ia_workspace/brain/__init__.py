"""Cerebro compartido: memoria persistente en Markdown, por proyecto y global.

``model`` (formato y validación), ``store`` (archivos con rutas seguras), ``service``
(localizar → seleccionar → leer) y ``cli`` (consulta de solo lectura para los agentes).
El índice SQLite vive en ``persistence.brain_index``.
"""
