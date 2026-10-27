-- Schema v5: composition and premiere dates of works (classical_core/dates.py; DEVIATIONS.md, "Work dates").
-- work_date: what each automatic source says about a work, one row per (work, kind, source). Sources are
--   'tags' (an explicit COMPOSITIONDATE tag), 'musicbrainz' (composer relationship dates, premiere events)
--   and 'dataset' (the bundled premieres.json). `date_from`/`date_to` are partial ISO dates ('1804', '1805-11-20');
--   `precision` is day | month | year | decade | circa. A user's correction is an `override` row
--   (entity 'work', op 'dates') and wins over all of these.
-- work gains the chosen values with their source (composed_start/composed_end exist since v1), and
--   dates_mb_at: when the MusicBrainz answer for the work was last read (the dates job's incremental cursor).
CREATE TABLE work_date (
    work_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('composed', 'premiered')),
    source TEXT NOT NULL CHECK (source IN ('tags', 'musicbrainz', 'dataset')),
    date_from TEXT NOT NULL, date_to TEXT, precision TEXT NOT NULL, place TEXT, ref TEXT, at REAL NOT NULL,
    PRIMARY KEY (work_id, kind, source)
);
ALTER TABLE work ADD COLUMN composed_precision TEXT;
ALTER TABLE work ADD COLUMN composed_source TEXT;
ALTER TABLE work ADD COLUMN premiered TEXT;
ALTER TABLE work ADD COLUMN premiered_precision TEXT;
ALTER TABLE work ADD COLUMN premiered_place TEXT;
ALTER TABLE work ADD COLUMN premiered_source TEXT;
ALTER TABLE work ADD COLUMN dates_mb_at REAL
