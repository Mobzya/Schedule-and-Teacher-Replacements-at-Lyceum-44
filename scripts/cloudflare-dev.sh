#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

UV_EXECUTABLE="$(command -v uv || true)"
if [[ -z "$UV_EXECUTABLE" && -x "$HOME/.local/bin/uv" ]]; then
  UV_EXECUTABLE="$HOME/.local/bin/uv"
fi
[[ -n "$UV_EXECUTABLE" ]] || { printf 'Ошибка: сначала установи uv.\n' >&2; exit 1; }
UV_BIN_DIRECTORY="$(dirname -- "$UV_EXECUTABLE")"
case ":$PATH:" in
  *":$UV_BIN_DIRECTORY:"*) ;;
  *) PATH="$UV_BIN_DIRECTORY:$PATH"; export PATH ;;
esac

TEMP_REQUIREMENTS_BACKUP=""
restore_requirements() {
  if [[ -n "$TEMP_REQUIREMENTS_BACKUP" && -f "$TEMP_REQUIREMENTS_BACKUP" ]]; then
    mv -- "$TEMP_REQUIREMENTS_BACKUP" "$PROJECT_ROOT/requirements.txt"
  fi
}
trap restore_requirements EXIT

if [[ -f "$PROJECT_ROOT/requirements.txt" ]]; then
  TEMP_REQUIREMENTS_BACKUP="$(mktemp)"
  mv -- "$PROJECT_ROOT/requirements.txt" "$TEMP_REQUIREMENTS_BACKUP"
fi

npx wrangler d1 migrations apply lyceum44-schedule --local
printf '%s\n' 'Dev не загружает значения из .env. Для Telegram-тестов используй отдельные тестовые переменные, не production token.'
env -u VIRTUAL_ENV CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV=false "$UV_EXECUTABLE" run pywrangler dev "$@"
