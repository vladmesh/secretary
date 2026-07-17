# systemd assets

Будущие units нового контура. Эти файлы не устанавливаются автоматически и не меняют
живые сервисы. Пути соответствуют production layout `~/secretary`,
`~/secretary-instance`, `~/secretary-data`; installation-specific renderer/reconcile
должен проверить их перед cutover.

`secretary-memory.service` запускает MCP из `secretary[memory]`, сохраняя endpoint
`127.0.0.1:8077/mcp` и текущую production-модель. Старый `memory-mcp.service` остаётся
владельцем порта до операторского переключения.
