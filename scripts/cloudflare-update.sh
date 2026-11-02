#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--dry-run" ) ]]; then
  printf 'Использование: bash scripts/cloudflare-update.sh [--dry-run]\n' >&2
  exit 2
fi

UV_EXECUTABLE="$(command -v uv || true)"
if [[ -z "$UV_EXECUTABLE" && -x "$HOME/.local/bin/uv" ]]; then
  UV_EXECUTABLE="$HOME/.local/bin/uv"
fi
[[ -n "$UV_EXECUTABLE" ]] || { printf 'Сначала установите uv.\n' >&2; exit 1; }
PATH="$(dirname -- "$UV_EXECUTABLE"):$PATH"
export PATH

TEMP_REQUIREMENTS_BACKUP=""
restore_requirements() {
  if [[ -n "$TEMP_REQUIREMENTS_BACKUP" && -f "$TEMP_REQUIREMENTS_BACKUP" ]]; then
    mv -- "$TEMP_REQUIREMENTS_BACKUP" "$PROJECT_ROOT/requirements.txt"
  fi
}
trap restore_requirements EXIT

# pywrangler no longer permits requirements.txt next to pyproject.toml.
# Keep the local/Render requirements and restore them even after a failed build.
if [[ -f requirements.txt ]]; then
  TEMP_REQUIREMENTS_BACKUP="$(mktemp)"
  mv -- requirements.txt "$TEMP_REQUIREMENTS_BACKUP"
fi

env -u VIRTUAL_ENV "$UV_EXECUTABLE" run --group dev pywrangler deploy --dry-run
if [[ $# -eq 0 ]]; then
  # New code may need new tables. Wrangler tracks applied migrations, so this
  # is safe to repeat; dry-run never changes the remote database.
  npx wrangler d1 migrations apply DB --remote
  env -u VIRTUAL_ENV "$UV_EXECUTABLE" run --group dev pywrangler deploy
  printf '\nWorker обновлён. Секреты и Telegram webhook сохранены.\n'
fi
