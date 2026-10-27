-- Schema v4: one recording across the separate MA albums of a multi-disc set (docs: DEVIATIONS.md, "Cross-disc").
-- recording.joined_to: set on a later disc's recording when it continues an earlier disc's (the lead); such a
--   recording is hidden everywhere and its slots sit on the lead. own_json keeps the lead's own summary fields
--   (recorded range, complete, evidence) so a join can be undone without re-deriving the lead's album.
--   parts_total: the work's part count as the derivation knew it (MB parts or the movement-total tag).
-- recording_slot.home_id / home_ord: the recording and position the slot was derived into; recording_id and
--   ord are where it plays now (the lead, in disc order). part_key / section: the movement identity and opera
--   act the join pass compares across discs.
-- album_info.set_key: the album name without its disc marker plus the album artist; disc_no / disc_total /
--   release_mbid: the album's disc evidence (title marker, DISCNUMBER/TOTALDISCS, MusicBrainz release).
ALTER TABLE recording ADD COLUMN joined_to TEXT;
ALTER TABLE recording ADD COLUMN parts_total INTEGER;
ALTER TABLE recording ADD COLUMN own_json TEXT;
CREATE INDEX recording_joined ON recording(joined_to) WHERE joined_to IS NOT NULL;
ALTER TABLE recording_slot ADD COLUMN home_id TEXT;
ALTER TABLE recording_slot ADD COLUMN home_ord INTEGER;
ALTER TABLE recording_slot ADD COLUMN part_key TEXT;
ALTER TABLE recording_slot ADD COLUMN section INTEGER;
UPDATE recording_slot SET home_id=recording_id, home_ord=ord;
CREATE INDEX recording_slot_home ON recording_slot(home_id);
ALTER TABLE album_info ADD COLUMN set_key TEXT;
ALTER TABLE album_info ADD COLUMN disc_no INTEGER;
ALTER TABLE album_info ADD COLUMN disc_total INTEGER;
ALTER TABLE album_info ADD COLUMN release_mbid TEXT;
CREATE INDEX album_info_set ON album_info(set_key) WHERE set_key IS NOT NULL;
CREATE INDEX album_info_release ON album_info(release_mbid) WHERE release_mbid IS NOT NULL
