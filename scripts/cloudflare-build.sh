#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_ROOT="$PROJECT_ROOT/python_modules/app"

mkdir -p "$MODULE_ROOT/services"
cp "$PROJECT_ROOT/app/__init__.py" \
  "$PROJECT_ROOT/app/config.py" \
  "$PROJECT_ROOT/app/db.py" \
  "$PROJECT_ROOT/app/domain.py" \
  "$PROJECT_ROOT/app/messaging.py" \
  "$PROJECT_ROOT/app/parsers.py" \
  "$PROJECT_ROOT/app/source_urls.py" \
  "$PROJECT_ROOT/app/ui.py" \
  "$PROJECT_ROOT/app/utils.py" \
  "$MODULE_ROOT/"
cp "$PROJECT_ROOT/app/services/__init__.py" \
  "$PROJECT_ROOT/app/services/avatars.py" \
  "$PROJECT_ROOT/app/services/calendar.py" \
  "$PROJECT_ROOT/app/services/friends.py" \
  "$PROJECT_ROOT/app/services/notifications.py" \
  "$PROJECT_ROOT/app/services/schedule.py" \
  "$PROJECT_ROOT/app/services/sync.py" \
  "$MODULE_ROOT/services/"
