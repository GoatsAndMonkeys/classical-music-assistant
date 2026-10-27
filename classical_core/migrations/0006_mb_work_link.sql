-- Schema v6: linking works to MusicBrainz works (classical_core/work_link.py; DEVIATIONS.md, "MusicBrainz work links").
-- mb_work_match: the linking job's last decision per work. `sig` covers what the decision read (matcher version,
--   title, key, catalogue, the composer's MusicBrainz id): a work is looked at again only when it changes.
--   state: 'linked' (the job added alias(work, mbid)), 'review' (candidates listed for a person), 'none' (no
--   candidate), 'skip' (not eligible: an arrangement, a part, no composer id), 'user' (a person decided).
--   A link is an `alias` row like any other MusicBrainz work id; a person's decision is an `override` row
--   (entity 'work', op 'mb_link') and wins over the job.
CREATE TABLE mb_work_match (
    work_id TEXT PRIMARY KEY, sig TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('linked', 'review', 'none', 'skip', 'user')),
    mbid TEXT, how TEXT, detail_json TEXT, at REAL NOT NULL
)
