---
name: colab-run
description: Открыть Google Colab в удалённом браузере vladmesh, вписать код в ячейку через Monaco API, запустить и забрать вывод — весь ввод через DOM, без OS-клавиш. Триггеры — «прогони в Colab», «запусти код в колабе», «Colab-ноутбук».
---

# Colab-run — выполнить код в Google Colab

Открыть/создать Colab-ноутбук под аккаунтом vladmesh, вписать код, запустить, вернуть вывод.
Весь ввод — через DOM (`eval`), НЕ через computer-use OS-клавиши (на ноуте это триггерит
airplane mode, см. [[remote-browser]]).

## Предпосылки

- Активное окружение `vladmesh-local` (скилл [[remote-browser]]).
- Залогиненная Google-сессия в этом браузере (скилл [[google-session-transfer]]). Без неё Colab
  редиректит на логин, который в автобраузере не пройти.

## Открыть ноутбук

```
orca goto --url "https://colab.research.google.com/#create=true" --environment vladmesh-local
```

`#create=true` создаёт новый ноутбук — открытие без редиректа на логин уже подтверждает, что
сессия жива.

## Вписать код (Monaco API, не набор клавиш)

Colab-редактор — Monaco. `inserttext` и CDP `type` по a11y-обёртке НЕ вписывают текст в него.
Работает только через сам Monaco API:

```
orca eval --environment vladmesh-local --expression '
  const ed = window.monaco.editor.getEditors()
    .find(e => e.getDomNode().closest("div.cell"));
  ed.getModel().setValue(`print("hello world")`);
'
```

Код-строку экранировать под JS-литерал (бэктики/кавычки/переносы). Несколько ячеек — по одному
редактору на `div.cell`; целиться в нужный по индексу/содержимому.

## Запустить и забрать вывод

- Запуск: `snapshot` ради свежего ref кнопки «Run cell» (ref-ы эфемерны, см. [[remote-browser]]),
  затем `orca click --element <ref> --environment vladmesh-local`. Рантайм подцепляется сам.
- Дать выполниться (poll, не слепой sleep), затем вывод точечным eval, не полным snapshot:

```
orca eval --environment vladmesh-local --expression '
  [...document.querySelectorAll("div.output-content")].map(e => e.innerText)
'
```

Референс — прогон 2026-07-04: `print("hello world")` выполнен за 0.037s, вывод `hello world`.

## Токен-гигиена

DOM-first: snapshot только ради ref-ов кнопок, данные (код в ячейке, вывод) — точечным eval.
Полный snapshot Colab-страницы тяжёлый, не дампить. Скриншот — только если визуально что-то не
сходится.
