-- Schema v1 (PLAN.md §4): core durable and derived tables for M1a.
-- Durable: composer, person, work (ids and first spellings), alias, redirect, split_*, correction_log,
--          override, grouping_constraint, occurrence, mapping_state, provider_seen, job, mb_cache.
-- Derived: evidence-derived groupings (recording, recording_slot, work_link, track_credit), review_item,
--          and the album/track display caches. Evidence is durable input, replaced per origin.

CREATE TABLE composer (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT NOT NULL,
    born TEXT, died TEXT, nationality TEXT, era TEXT, image_ref TEXT
);
CREATE TABLE person (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'person', instrument TEXT, born TEXT, died TEXT, image_ref TEXT
);
CREATE TABLE work (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, sort_title TEXT NOT NULL, work_type TEXT NOT NULL DEFAULT 'Other',
    key TEXT, catalogue_display TEXT, catalogue_sort TEXT, composed_start TEXT, composed_end TEXT,
    parent_work_id TEXT, part_index INTEGER, relation TEXT NOT NULL DEFAULT 'original', derived_from_work_id TEXT,
    identity_tier TEXT NOT NULL DEFAULT 'title', created_at REAL NOT NULL
);
CREATE TABLE work_composer (work_id TEXT NOT NULL, composer_id TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (work_id, composer_id));
CREATE INDEX work_composer_c ON work_composer(composer_id);
CREATE TABLE work_credit (work_id TEXT NOT NULL, person_id TEXT NOT NULL, role TEXT NOT NULL,
                          PRIMARY KEY (work_id, person_id, role));
CREATE TABLE alias (entity TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (entity, kind, value));
CREATE INDEX alias_id ON alias(entity, entity_id);
CREATE TABLE redirect (entity TEXT NOT NULL, old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, reason TEXT, at REAL,
                       CHECK (old_id <> new_id));
CREATE TABLE split_event (id TEXT PRIMARY KEY, entity TEXT NOT NULL, original_id TEXT NOT NULL,
                          surviving_id TEXT NOT NULL, at REAL, CHECK (surviving_id = original_id));
CREATE TABLE split_child (split_event_id TEXT NOT NULL, child_id TEXT NOT NULL, occurrence_ids_json TEXT NOT NULL);
CREATE TABLE correction_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, payload_json TEXT NOT NULL,
                             user TEXT, at REAL NOT NULL);
CREATE TABLE override (id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                       op TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL, by_user TEXT);
CREATE TABLE grouping_constraint (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('separate', 'together')),
                                  occurrence_ids_json TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE occurrence (
    id TEXT PRIMARY KEY, album_library_id TEXT NOT NULL, disc INTEGER, position INTEGER,
    library_track_id TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1,
    UNIQUE (album_library_id, library_track_id)
);
CREATE INDEX occurrence_track ON occurrence(library_track_id);
CREATE TABLE mapping_state (
    provider_instance TEXT NOT NULL, item_id TEXT NOT NULL, library_track_id TEXT NOT NULL,
    file_fingerprint TEXT, tag_hash TEXT, mbid TEXT, indexed_at REAL,
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'read_error', 'unavailable', 'gone')),
    error TEXT, retry_after REAL,
    PRIMARY KEY (provider_instance, item_id)
);
CREATE INDEX mapping_state_track ON mapping_state(library_track_id);
CREATE TABLE provider_seen (provider_instance TEXT PRIMARY KEY,
                            state TEXT NOT NULL CHECK (state IN ('configured', 'unavailable', 'removal_pending', 'removed')),
                            last_change REAL);
CREATE TABLE job (id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, cursor TEXT,
                  state TEXT NOT NULL, started_at REAL, error TEXT);
CREATE TABLE mb_cache (url TEXT PRIMARY KEY, status INTEGER NOT NULL, body_zlib BLOB, fetched_at REAL NOT NULL,
                       expires_at REAL NOT NULL);

-- evidence: one row per assertion; `origin` is what one refresh replaces as a unit, `edge` its stable key
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT NOT NULL,
    value TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL, edge TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0, observed_at REAL NOT NULL, retracted_at REAL
);
CREATE INDEX evidence_entity ON evidence(entity, entity_id) WHERE retracted_at IS NULL;
CREATE INDEX evidence_origin ON evidence(source_ref) WHERE retracted_at IS NULL;

-- derived
CREATE TABLE recording (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, album_library_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'auto', recorded_start TEXT, recorded_end TEXT, recorded_precision TEXT,
    venue TEXT, label TEXT, catalog_no TEXT, release_year INTEGER, tier TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
    evidence_fields INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX recording_work ON recording(work_id);
CREATE INDEX recording_album ON recording(album_library_id);
CREATE TABLE recording_slot (recording_id TEXT NOT NULL, occurrence_id TEXT NOT NULL UNIQUE,
                             movement_no INTEGER, movement_name TEXT, ord INTEGER NOT NULL);
CREATE INDEX recording_slot_r ON recording_slot(recording_id, ord);
CREATE TABLE work_link (occurrence_id TEXT NOT NULL, work_id TEXT NOT NULL, ord INTEGER NOT NULL,
                        partial INTEGER NOT NULL DEFAULT 0, live INTEGER NOT NULL DEFAULT 0,
                        medley INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, source_ref TEXT,
                        PRIMARY KEY (occurrence_id, work_id));
CREATE INDEX work_link_w ON work_link(work_id);
CREATE TABLE track_credit (occurrence_id TEXT NOT NULL, person_id TEXT NOT NULL, role TEXT NOT NULL,
                           instrument TEXT, ord INTEGER NOT NULL);
CREATE INDEX track_credit_o ON track_credit(occurrence_id);
CREATE TABLE review_item (id TEXT PRIMARY KEY, reason TEXT NOT NULL, album_library_id TEXT,
                          entity TEXT, entity_id TEXT, detail_json TEXT NOT NULL);
CREATE INDEX review_item_album ON review_item(album_library_id);
CREATE TABLE album_info (album_library_id TEXT PRIMARY KEY, name TEXT, artist TEXT, year INTEGER, image_json TEXT,
                         ambiguous INTEGER NOT NULL DEFAULT 0, indexed_at REAL,
                         signature TEXT);
CREATE TABLE track_info (library_track_id TEXT PRIMARY KEY, title TEXT, artist TEXT, duration INTEGER);
