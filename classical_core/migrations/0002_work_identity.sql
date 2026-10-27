-- Schema v2: cross-album work identity and nested credit details (DEVIATIONS.md, "Cross-album works").
-- recording.source_work_id: the album's own work before a cross-album merge redirected it (a user can
--   keep it separate again); work.work_type_rank: how strong the evidence behind work_type is (-1 = none
--   recorded yet, so the next derivation replaces it); track_credit.detail: 'Pater Profundus' in
--   'Ain Anger (bass (Pater Profundus))'.
ALTER TABLE recording ADD COLUMN source_work_id TEXT;
ALTER TABLE track_credit ADD COLUMN detail TEXT;
ALTER TABLE work ADD COLUMN work_type_rank INTEGER NOT NULL DEFAULT -1;
