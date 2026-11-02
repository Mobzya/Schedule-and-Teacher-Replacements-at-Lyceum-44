CREATE TABLE IF NOT EXISTS friends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  label_key TEXT NOT NULL,
  class_name TEXT NOT NULL,
  profile TEXT,
  academic_year TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_id, label_key)
);
