-- Schema v8: box sets (DEVIATIONS.md "Box sets"; classical_core/boxes.py).
-- album_info.box_id: the box this album may be a disc of (or be on its own), a hash of its key (MB release,
--   box title before a 'CD 061' marker plus album artist, set key with a disc tag, or the album itself when its
--   tracks span many discs). box_title / box_disc / box_total: the box's title, this album's disc in it, and the
--   disc total, as far as the album says. Whether a group of albums IS a box is decided when it is read, so it
--   follows corrections and imports without a recompute. Filled when an album is derived (DERIVE_VERSION 10).
-- The 'box' override (op 'not_box') is a user's "not a box set".
ALTER TABLE album_info ADD COLUMN box_id TEXT;
ALTER TABLE album_info ADD COLUMN box_title TEXT;
ALTER TABLE album_info ADD COLUMN box_disc INTEGER;
ALTER TABLE album_info ADD COLUMN box_total INTEGER;
CREATE INDEX album_info_box ON album_info(box_id) WHERE box_id IS NOT NULL
