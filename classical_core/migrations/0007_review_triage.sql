-- Schema v7: review triage (DEVIATIONS.md "Review triage").
-- review_dismissal: durable "not now" on a review item. review_item rows are derived and get new ids on every
--   derive, so the key is a fingerprint of what the item says (classical_core/triage.py fingerprint()); an
--   item whose evidence changes gets a new key and comes back.
-- review_batch: the undo log. One row per decision request (one card's action, a bulk dismiss, or a single
--   correction from another page); undo_json holds the inverse steps, targets_json what it touched (a later
--   batch on the same target must be undone first).
-- review_item(reason): the triage view and the continuation lookup filter by reason.
CREATE TABLE review_dismissal (
    key TEXT PRIMARY KEY, reason TEXT NOT NULL, album_library_id TEXT, by_user TEXT, at REAL NOT NULL,
    batch INTEGER
);
CREATE TABLE review_batch (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, label TEXT NOT NULL, by_user TEXT,
    at REAL NOT NULL, undone_at REAL, actions INTEGER NOT NULL DEFAULT 1,
    undo_json TEXT NOT NULL, targets_json TEXT NOT NULL
);
CREATE INDEX review_batch_user ON review_batch(by_user, seq);
CREATE INDEX review_item_reason ON review_item(reason)
