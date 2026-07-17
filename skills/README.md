# Ролевые скиллы

Канон скиллов хранится по ролям, а не по оболочкам:

- `skills/roles/secretary/*`
- `skills/roles/curator/*`
- `skills/roles/retro/*`
- `skills/roles/steward/*`

Оболочки получают копии из канона. Симлинки здесь не используются: часть рантаймов зеркалит home
и может потерять ссылку или прочитать её в другом контексте. Копии проще проверять хешами.

Проверка:

```bash
python3 /home/dev/control-panel/scripts/role_skills.py audit --json
```

Синхронизация:

```bash
python3 /home/dev/control-panel/scripts/role_skills.py sync
```

Список ролей, навыков и целевых директорий задаёт `skills/manifest.toml`. Если скилл нужен роли,
добавляй его в `skills/roles/<role>/` и в манифест. Не дублируй вручную между Claude, Codex и
Hermes. Для одной оболочки разные target-группы могут использовать один root, но не вложенные
root-каталоги: рекурсивное обнаружение скиллов смешивает namespace и выдаёт неверные locator'ы.
