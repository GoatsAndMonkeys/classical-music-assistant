-- Schema v10: the visibility check (Index._VIS_OCC) reads a track's mappings by status and provider; covering
-- them in the track index answers it from the index alone (the Albums lists check every recording). The old
-- one-column index is a prefix of the new one.
CREATE INDEX IF NOT EXISTS mapping_state_vis ON mapping_state(library_track_id, status, provider_instance);
DROP INDEX IF EXISTS mapping_state_track
