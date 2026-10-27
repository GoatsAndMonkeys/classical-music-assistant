-- Schema v3: accolades, review links and the general web cache (docs/accolades.md, PLAN.md §5.8, §5.9).
-- accolade: derived. Bundled-dataset rows (source 'dataset') are rebuilt by the accolades job and cleared with
--   the album's other derived rows; network rows (wikidata, critiquebrainz, discogs) are replaced per album by
--   the enrichment job. `target` is 'recording' (recording_id set) or 'album'; `grade` says how the match was
--   made: this_release | other_release | confirmed | uncertain (uncertain rows are never shown, only queued
--   for review). `expires_at` hides data whose licence wants it refreshed (Discogs: 6 hours).
-- accolade_decision: durable user decisions on a dataset match, keyed by the stable entry id, the album and
--   the work (recording ids are derived and can change).
-- link: external pages about an album (MusicBrainz url relations), replaced per album and source.
-- web_cache: the MB cache's twin for every other host (Wikidata, CritiqueBrainz, Discogs).
-- album_info gains the MA-side identifiers the enrichment job reads (kept out of the album signature).
CREATE TABLE accolade (
    id TEXT PRIMARY KEY, target TEXT NOT NULL CHECK (target IN ('recording', 'album')),
    recording_id TEXT, album_library_id TEXT NOT NULL, work_id TEXT,
    kind TEXT NOT NULL, award TEXT, category TEXT, year INTEGER, result TEXT,
    publication TEXT, reviewer TEXT, rating REAL, rating_scale REAL, rating_count INTEGER,
    url TEXT, source TEXT NOT NULL, source_ref TEXT NOT NULL, grade TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0, why_json TEXT, excerpt TEXT, excerpt_license TEXT,
    fetched_at REAL NOT NULL, expires_at REAL
);
CREATE INDEX accolade_rec ON accolade(recording_id);
CREATE INDEX accolade_album ON accolade(album_library_id);
CREATE TABLE accolade_decision (
    entry_ref TEXT NOT NULL, album_library_id TEXT NOT NULL, work_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirm', 'reject')), by_user TEXT, at REAL NOT NULL,
    PRIMARY KEY (entry_ref, album_library_id, work_id)
);
CREATE TABLE link (entity TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
                   source TEXT NOT NULL, PRIMARY KEY (entity, entity_id, url));
CREATE TABLE web_cache (url TEXT PRIMARY KEY, status INTEGER NOT NULL, body_zlib BLOB, fetched_at REAL NOT NULL,
                        expires_at REAL NOT NULL);
ALTER TABLE album_info ADD COLUMN label TEXT;
ALTER TABLE album_info ADD COLUMN barcode TEXT;
ALTER TABLE album_info ADD COLUMN mb_release TEXT;
ALTER TABLE album_info ADD COLUMN mb_release_group TEXT;
ALTER TABLE album_info ADD COLUMN discogs_release TEXT;
ALTER TABLE album_info ADD COLUMN enriched_at REAL;
