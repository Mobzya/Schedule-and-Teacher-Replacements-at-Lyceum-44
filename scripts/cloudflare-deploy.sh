#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
D1_DATABASE="lyceum44-schedule"
CONFIG_PATH="$PROJECT_ROOT/wrangler.jsonc"
TEMP_DEPLOY_LOG=""
TEMP_REQUIREMENTS_BACKUP=""

cleanup() {
  unset BOT_TOKEN ADMIN_IDS WEBHOOK_SECRET CF_WORKER_URL
  if [[ -n "$TEMP_REQUIREMENTS_BACKUP" && -f "$TEMP_REQUIREMENTS_BACKUP" ]]; then
    mv -- "$TEMP_REQUIREMENTS_BACKUP" "$PROJECT_ROOT/requirements.txt"
  fi
  if [[ -n "$TEMP_DEPLOY_LOG" && -f "$TEMP_DEPLOY_LOG" ]]; then
    rm -f -- "$TEMP_DEPLOY_LOG"
  fi
}
trap cleanup EXIT

fail() {
  printf 'Ошибка: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Не найдена команда '$1'. Установи её и запусти скрипт снова."
}

deploy_worker() {
  TEMP_DEPLOY_LOG="$(mktemp)"
  local requirements_file="$PROJECT_ROOT/requirements.txt"
  local deploy_succeeded=0
  if [[ -f "$requirements_file" ]]; then
    TEMP_REQUIREMENTS_BACKUP="$(mktemp)"
    mv -- "$requirements_file" "$TEMP_REQUIREMENTS_BACKUP"
  fi
  if env -u VIRTUAL_ENV "$UV_EXECUTABLE" run pywrangler deploy 2>&1 | tee "$TEMP_DEPLOY_LOG"; then
    deploy_succeeded=1
  fi
  if [[ -n "$TEMP_REQUIREMENTS_BACKUP" && -f "$TEMP_REQUIREMENTS_BACKUP" ]]; then
    mv -- "$TEMP_REQUIREMENTS_BACKUP" "$requirements_file"
    TEMP_REQUIREMENTS_BACKUP=""
  fi
  if [[ "$deploy_succeeded" != 1 ]]; then
    fail "Деплой завершился с ошибкой. См. вывод выше."
  fi
  local parsed_worker_url
  parsed_worker_url="$(grep -Eo 'https://[[:alnum:]._-]+\.workers\.dev' "$TEMP_DEPLOY_LOG" | tail -n 1 || true)"
  if [[ -n "$parsed_worker_url" ]]; then
    CF_WORKER_URL="$parsed_worker_url"
  fi
  rm -f -- "$TEMP_DEPLOY_LOG"
  TEMP_DEPLOY_LOG=""
}

bind_existing_d1() {
  local database_id="$1"
  python3 - "$CONFIG_PATH" "$D1_DATABASE" "$database_id" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
database_name, database_id = sys.argv[2:4]
config = json.loads(config_path.read_text())
binding = next((item for item in config.get("d1_databases", []) if item.get("binding") == "DB"), None)
if binding is None:
    raise SystemExit("В wrangler.jsonc не найден D1 binding DB")
binding["database_name"] = database_name
binding["database_id"] = database_id
binding["migrations_dir"] = "cloudflare/migrations"
fd, temp_name = tempfile.mkstemp(prefix="wrangler.", suffix=".jsonc", dir=config_path.parent)
try:
    with os.fdopen(fd, "w") as temp_file:
        json.dump(config, temp_file, ensure_ascii=False, indent=2)
        temp_file.write("\n")
    os.replace(temp_name, config_path)
except Exception:
    Path(temp_name).unlink(missing_ok=True)
    raise
PY
}

preflight_worker() {
  local requirements_file="$PROJECT_ROOT/requirements.txt"
  local preflight_succeeded=0
  if [[ -f "$requirements_file" ]]; then
    TEMP_REQUIREMENTS_BACKUP="$(mktemp)"
    mv -- "$requirements_file" "$TEMP_REQUIREMENTS_BACKUP"
  fi
  if env -u VIRTUAL_ENV "$UV_EXECUTABLE" run pywrangler deploy --dry-run; then
    preflight_succeeded=1
  fi
  if [[ -n "$TEMP_REQUIREMENTS_BACKUP" && -f "$TEMP_REQUIREMENTS_BACKUP" ]]; then
    mv -- "$TEMP_REQUIREMENTS_BACKUP" "$requirements_file"
    TEMP_REQUIREMENTS_BACKUP=""
  fi
  [[ "$preflight_succeeded" == 1 ]] || fail "Проверочная сборка Worker завершилась с ошибкой; база и секреты не изменены."
}

cd "$PROJECT_ROOT"

[[ -f "$CONFIG_PATH" ]] || fail "Запусти скрипт из копии проекта с wrangler.jsonc."
for command_name in node npm npx curl python3; do
  require_command "$command_name"
done

UV_EXECUTABLE="$(command -v uv || true)"
if [[ -z "$UV_EXECUTABLE" ]]; then
  printf 'uv не найден; устанавливаю его официальным установщиком Astral…\n'
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV_EXECUTABLE="$(command -v uv || true)"
  if [[ -z "$UV_EXECUTABLE" && -x "$HOME/.local/bin/uv" ]]; then
    UV_EXECUTABLE="$HOME/.local/bin/uv"
  fi
  [[ -n "$UV_EXECUTABLE" ]] || fail "uv установлен, но исполняемый файл не найден. Открой новый терминал и повтори запуск."
fi
UV_BIN_DIRECTORY="$(dirname -- "$UV_EXECUTABLE")"
case ":$PATH:" in
  *":$UV_BIN_DIRECTORY:"*) ;;
  *) PATH="$UV_BIN_DIRECTORY:$PATH"; export PATH ;;
esac

if ! command -v openssl >/dev/null 2>&1; then
  printf 'Предупреждение: openssl не найден; секрет webhook будет создан Python.\n'
fi

printf '%s\n' 'Этот скрипт не читает .env и не записывает секреты в файлы проекта.'
printf '%s\n' 'Перед продолжением останови локальный polling и старый сервис Render, если они ещё запущены.'
read -r -p 'Они остановлены? Введи STOP для продолжения: ' STOP_CONFIRMATION
[[ "$STOP_CONFIRMATION" == "STOP" ]] || fail "Остановлено. Сначала останови прежний экземпляр бота."

read -r -p 'Telegram ID администратора (несколько ID через запятую): ' ADMIN_IDS
[[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || fail "ADMIN_IDS должен содержать числовые Telegram ID через запятую."
read -r -s -p 'Новый токен бота из BotFather: ' BOT_TOKEN
printf '\n'
[[ -n "$BOT_TOKEN" ]] || fail "Токен не может быть пустым."
WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

if ! npx wrangler whoami >/dev/null 2>&1; then
  npx wrangler login
fi

if grep -q 'REPLACE_WITH_D1_DATABASE_ID' "$CONFIG_PATH"; then
  printf 'Проверяю, существует ли D1 %s…\n' "$D1_DATABASE"
  D1_LIST_JSON="$(npx wrangler d1 list --json)"
  D1_ID="$(python3 -c 'import json,sys; data=json.load(sys.stdin); rows=data if isinstance(data,list) else data.get("result",[]); row=next((r for r in rows if r.get("name")==sys.argv[1]),{}); print(row.get("uuid") or row.get("database_id") or row.get("id") or "")' "$D1_DATABASE" <<<"$D1_LIST_JSON")"
  if [[ -n "$D1_ID" ]]; then
    printf 'Нашёл готовую D1; записываю её ID в wrangler.jsonc…\n'
    bind_existing_d1 "$D1_ID"
  else
    printf 'Создаю D1 и добавляю binding DB в wrangler.jsonc…\n'
    npx wrangler d1 create "$D1_DATABASE" --binding DB --update-config
    D1_LIST_JSON="$(npx wrangler d1 list --json)"
    D1_ID="$(python3 -c 'import json,sys; data=json.load(sys.stdin); rows=data if isinstance(data,list) else data.get("result",[]); row=next((r for r in rows if r.get("name")==sys.argv[1]),{}); print(row.get("uuid") or row.get("database_id") or row.get("id") or "")' "$D1_DATABASE" <<<"$D1_LIST_JSON")"
    [[ -n "$D1_ID" ]] || fail "D1 создана, но Wrangler не вернул её ID. Запусти скрипт повторно; он найдёт готовую базу."
    bind_existing_d1 "$D1_ID"
  fi
else
  printf 'D1 ID уже указан; проверяю базу %s…\n' "$D1_DATABASE"
  npx wrangler d1 info "$D1_DATABASE" >/dev/null
fi

printf 'Устанавливаю Python-зависимости…\n'
env -u VIRTUAL_ENV "$UV_EXECUTABLE" sync

printf 'Проверяю совместимость и собираемость Worker без публикации…\n'
preflight_worker

printf 'Применяю миграции D1…\n'
npx wrangler d1 migrations apply "$D1_DATABASE" --remote

# Secret values are put only after the initial script exists in the account.
printf 'Создаю начальную версию Worker…\n'
deploy_worker
[[ -n "$CF_WORKER_URL" ]] || read -r -p 'Вставь URL Worker из вывода деплоя: ' CF_WORKER_URL
[[ "$CF_WORKER_URL" =~ ^https://[[:alnum:]._-]+\.workers\.dev/?$ ]] || fail "Ожидался адрес Cloudflare вида https://имя.поддомен.workers.dev"
CF_WORKER_URL="${CF_WORKER_URL%/}"

printf 'Загружаю секреты в Cloudflare (значения не сохраняются в репозитории)…\n'
printf '%s' "$BOT_TOKEN" | npx wrangler secret put BOT_TOKEN
printf '%s' "$ADMIN_IDS" | npx wrangler secret put ADMIN_IDS
printf '%s' "$WEBHOOK_SECRET" | npx wrangler secret put TELEGRAM_WEBHOOK_SECRET

printf 'Публикую финальную версию Worker…\n'
deploy_worker

printf 'Проверяю /health…\n'
HEALTH_RESPONSE="$(curl --fail --silent --show-error "$CF_WORKER_URL/health")" || fail "Worker не ответил на /health. Проверь деплой и логи Wrangler."
[[ "$HEALTH_RESPONSE" == "ok" ]] || fail "Неожиданный ответ /health: $HEALTH_RESPONSE"

printf 'Подключаю Telegram webhook…\n'
WEBHOOK_RESPONSE="$(curl --fail --silent --show-error \
  --config - <<EOF
url = "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook"
data-urlencode = "url=${CF_WORKER_URL}/telegram"
data-urlencode = "secret_token=${WEBHOOK_SECRET}"
data = "drop_pending_updates=true"
EOF
)" || fail "Telegram не принял setWebhook. Проверь токен и URL."
printf '%s\n' "$WEBHOOK_RESPONSE"
python3 -c 'import json,sys; result=json.load(sys.stdin); sys.exit(0 if result.get("ok") else 1)' <<<"$WEBHOOK_RESPONSE" \
  || fail "Telegram ответил ошибкой при регистрации webhook."

printf '\nГотово! Бот развернут: %s\n' "$CF_WORKER_URL"
printf 'Проверь /start в Telegram. Для логов выполни: npx wrangler tail lyceum44-schedule-bot\n'
