-- Schema v11: liner notes. `liner_note` caches what the work and performer pages show from Wikipedia and
-- Wikidata (a work's notes or an artist's bio, keyed "work:<mbid>" / "artist:<mbid>"; data_json null records
-- that there is none), refreshed after liner_notes.NOTES_TTL. `listening_guide` keeps the optional
-- model-written guide per MusicBrainz work, with the model that wrote it; a failed attempt is kept too, so a
-- work is asked about at most once (see liner_notes.guide_due).
CREATE TABLE IF NOT EXISTS liner_note (
  key TEXT PRIMARY KEY,
  data_json TEXT,
  fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listening_guide (
  work_mbid TEXT PRIMARY KEY,
  text TEXT,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at REAL NOT NULL
)
