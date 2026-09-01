# Бесплатный deploy на Render

Бот запускается как Render Free Web Service, а данные хранятся во внешнем PostgreSQL. Без `DATABASE_URL` бот тоже
запустится, но потеряет SQLite-данные при перезапуске.

## 1. PostgreSQL в Supabase

1. Создайте бесплатный Supabase project и сохраните пароль базы.
2. Нажмите `Connect` вверху панели проекта.
3. Выберите `Session pooler` (порт `5432`), потом скопируйте URI.
4. Замените `[YOUR-PASSWORD]` на пароль. Не вставляйте URI в GitHub и `.env.example`.

Для Render нужен именно IPv4-совместимый Session pooler. Строка имеет вид:

```text
postgresql://postgres.PROJECT:PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres
```

Если в пароле есть `@`, `:`, `/`, `#` или `%`, создайте новый пароль из букв и цифр либо URL-encode спецсимволы.

## 2. Render Blueprint

1. В Render Dashboard нажмите `New` → `Blueprint`.
2. Подключите GitHub-репозиторий `Mobzya/Schedule-and-Teacher-Replacements-at-Lyceum-44`.
3. Ветка: `main`; Blueprint path: `render.yaml`.
4. На экране secrets задайте:

   - `BOT_TOKEN` — новый token из `@BotFather`;
   - `ADMIN_IDS` — Telegram ID, например `123456789`;
   - `DATABASE_URL` — Session pooler URI из предыдущего шага.

5. Подтвердите deploy. При первом старте таблицы PostgreSQL создадутся автоматически.

## 3. Проверка

В Render Logs дожно появиться:

```text
Health server on 10000; publication=waiting
```

Откройте `https://<service>.onrender.com/health`, затем отправьте боту `/start` и `/admin`.

## 4. Предотвращение засыпания

Render Free засыпает после 15 минут без входящего HTTP-трафика. В любом uptime-monitor создайте HTTP GET для:

```text
https://<service>.onrender.com/health
```

Интервал: 10 минут. Не используйте `/robots.txt`: он не будит спящий Free Service.

## 5. Обновления и секреты

Push в `main` запускает новый deploy; PostgreSQL-данные при этом не исчезают. Никогда не печатайте в логах и не коммитьте
`BOT_TOKEN` и `DATABASE_URL`.
