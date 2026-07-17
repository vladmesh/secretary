---
name: google-session-transfer
description: Перенести залогиненную Google-сессию vladmesh в Orca serve-браузер на его ноуте через дешифровку и заливку Chrome-кук — обход блокировки SSO в автобраузере. Триггеры — «залогинь Google в удалённом браузере», «перенеси мою сессию», «Colab/Gmail под моим аккаунтом», подготовка к colab-run.
---

# Google-session-transfer — перенос кук вместо SSO

Google блокирует OAuth/SSO в автоматизированном браузере (палит CDP, «This browser or app may
not be secure»). Логин руками внутрь Orca-браузера невозможен. Обход — перенести cookie уже
залогиненной сессии из настоящего Chrome vladmesh. Предпосылка — активное окружение
`vladmesh-local` (скилл [[remote-browser]] уже подключён).

## Инварианты (жёсткие)

- **Только с явного разрешения vladmesh** брать его сессию — это его живые куки.
- **Лить ТОЛЬКО в ноутбучный serve-браузер** (`--environment vladmesh-local`), тот же домашний IP,
  что у Chrome. В VPS-native браузер (литовский датацентр-IP) НЕ лить — Google пометит вход
  подозрительным и уронит сессию.
- Куки — секрет: не логировать значения, не сохранять дольше сессии, профиль под задачу.
- Только интерактивная сессия секретаря, не воркер/автоматизация.

## Источник кук

Chrome/Chromium vladmesh: `~/snap/chromium/common/chromium/Profile 1/Cookies` (SQLite).
Запущен с `--password-store=basic` (иначе ключ в GNOME keyring, схема дешифровки другая).
Читать копию файла (основной может быть залочен живым Chrome).

## Дешифровка (Chrome на Linux, password-store=basic)

Значение куки в колонке `encrypted_value`, префикс `v10`:

- Ключ: `PBKDF2-HMAC-SHA1(password=b"peanuts", salt=b"saltysalt", iterations=1, dklen=16)`.
- Шифр: AES-128-CBC, IV = 16 пробелов (`b" " * 16`).
- Снять префикс `v10` (3 байта) перед расшифровкой.
- **Chrome ≥ 149**: в начало plaintext добавлены 32 байта `SHA256(domain)` — срезать первые 32
  байта расшифрованного (strip 32), иначе значение куки битое.

## Заливка в Orca-браузер

Через RPC Orca-браузера:

```
orca exec --command "cookies set <name> <value> --domain <d> --path <p> [--secure] [--httpOnly] --sameSite <s> --expires <unix>" --environment vladmesh-local
```

Нюансы имён и флагов:
- `__Host-` префикс: слать через `--url <origin>` без `--domain` (спецификация __Host- запрещает domain).
- `__Secure-` / `__Host-`: обязательно `--secure`.
- Время: `expires_utc` Chrome — микросекунды от 1601-01-01; unix = `expires_utc/1e6 - 11644473600`.

## Проверка

`orca goto --url https://myaccount.google.com --environment vladmesh-local`, затем точечный `eval`
(не полный snapshot — дорого) на признак залогиненности (наличие email/аватара в DOM). При успехе
целевой сервис (Colab, Gmail) откроется уже под аккаунтом.

Референс — прогон 2026-07-04: перенесено 132 google-cookie, 0 ошибок, Colab открылся залогиненным.

## Уборка

По завершении задачи — вычистить профиль/куки и снять окружение (`~/bin/remote-browser
disconnect`), как требует [[remote-browser]]. Живую Google-сессию не оставлять между сессиями
без явного «оставь, ещё погоняю».
