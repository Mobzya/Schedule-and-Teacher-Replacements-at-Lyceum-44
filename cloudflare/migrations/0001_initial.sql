CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  class_name TEXT,
  profile TEXT,
  academic_year TEXT,
  notifications INTEGER NOT NULL DEFAULT 1,
  reminder_minutes INTEGER NOT NULL DEFAULT 15,
  replacement_notifications INTEGER NOT NULL DEFAULT 1,
  morning_summary INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL,
  url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  valid_from TEXT,
  valid_until TEXT,
  expected_academic_year TEXT,
  last_status TEXT NOT NULL DEFAULT 'never',
  last_error TEXT,
  last_success_at TEXT,
  last_checked_at TEXT,
  content_hash TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weekly_lessons(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  class_name TEXT NOT NULL,
  profile TEXT,
  weekday INTEGER NOT NULL,
  lesson_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  room TEXT NOT NULL DEFAULT '',
  teacher TEXT,
  start_time TEXT,
  end_time TEXT,
  PRIMARY KEY(source_id,class_name,profile,weekday,lesson_number)
);
CREATE TABLE IF NOT EXISTS dated_lessons(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  lesson_date TEXT NOT NULL,
  class_name TEXT NOT NULL,
  profile TEXT,
  lesson_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  room TEXT NOT NULL DEFAULT '',
  teacher TEXT,
  start_time TEXT,
  end_time TEXT,
  PRIMARY KEY(source_id,lesson_date,class_name,profile,lesson_number)
);
CREATE TABLE IF NOT EXISTS replacements(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  lesson_date TEXT NOT NULL,
  class_name TEXT NOT NULL,
  lesson_number INTEGER NOT NULL,
  subject TEXT,
  teacher TEXT,
  room TEXT,
  cancelled INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  PRIMARY KEY(source_id,lesson_date,class_name,lesson_number)
);
CREATE TABLE IF NOT EXISTS school_calendar(
  day TEXT PRIMARY KEY,
  is_school_day INTEGER NOT NULL,
  schedule_weekday INTEGER,
  note TEXT,
  updated_by INTEGER,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_log(
  user_id INTEGER NOT NULL,
  notification_key TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY(user_id, notification_key)
);
CREATE TABLE IF NOT EXISTS notification_outbox(
  user_id INTEGER NOT NULL,
  notification_key TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  PRIMARY KEY(user_id, notification_key)
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER,
  action TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fsm_state(
  storage_key TEXT PRIMARY KEY,
  state TEXT,
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dated_lookup ON dated_lessons(lesson_date,class_name);
CREATE INDEX IF NOT EXISTS idx_weekly_lookup ON weekly_lessons(weekday,class_name);
CREATE INDEX IF NOT EXISTS idx_replacements_lookup ON replacements(lesson_date,class_name);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due ON notification_outbox(next_attempt_at,created_at);
