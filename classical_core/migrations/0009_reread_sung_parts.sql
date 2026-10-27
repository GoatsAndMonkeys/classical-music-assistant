-- Performer credits read before parts were told from instruments: 'Hans Hotter (Wotan)' was stored with
-- the role 'wotan', and 'Martha Mödl (Gutrune; 3rd Norn)' split at the ';' into two broken credits. The
-- stored role is lower-cased, so the part can no longer be told from an instrument ('erhu'): such files are
-- read again on the next sync: their fingerprint no longer matches, and their albums' signatures are cleared
-- so the sync plans them at all. A credit with an unbroken plain role is kept.
UPDATE mapping_state SET file_fingerprint = 'reread:0009'
WHERE status = 'ok' AND EXISTS (
  SELECT 1 FROM evidence e
  WHERE e.entity = 'mapping' AND e.entity_id = mapping_state.provider_instance || '/' || mapping_state.item_id
    AND e.field = 'performers' AND e.retracted_at IS NULL
    AND (length(json_extract(e.value, '$.name')) - length(replace(json_extract(e.value, '$.name'), '(', ''))
         != length(json_extract(e.value, '$.name')) - length(replace(json_extract(e.value, '$.name'), ')', ''))
         OR (json_extract(e.value, '$.kind') = 'person' AND json_extract(e.value, '$.detail') IS NULL
             AND json_extract(e.value, '$.role') NOT IN (
    'accordion', 'actor', 'alto', 'alto flute', 'alto-flute', 'announcer', 'band', 'bandoneon', 'baritone',
    'bass', 'bass baritone', 'bass clarinet', 'bass guitar', 'bass trombone', 'bass-baritone', 'bass-clarinet',
    'bass-guitar', 'bass-trombone', 'basset horn', 'basset-horn', 'bassoon', 'boy soprano', 'boy-soprano',
    'celesta', 'cello', 'choir', 'choirmaster', 'chorus', 'chorus master', 'chorus-master', 'clarinet',
    'commentator', 'concertmaster', 'conductor', 'continuo', 'contrabass', 'contrabassoon', 'contralto',
    'cor anglais', 'cor-anglais', 'cornet', 'countertenor', 'cymbals', 'double bass', 'double-bass', 'drums',
    'english horn', 'english-horn', 'ensemble', 'euphonium', 'flute', 'fortepiano', 'french horn',
    'french-horn', 'glockenspiel', 'guitar', 'harp', 'harpsichord', 'horn', 'host', 'keyboard', 'keyboards',
    'leader', 'lute', 'mandolin', 'marimba', 'mezzo', 'mezzo soprano', 'mezzo-soprano', 'narrator', 'oboe',
    'orchestra', 'organ', 'percussion', 'performer', 'piano', 'pianoforte', 'piccolo', 'presenter', 'quartet',
    'reciter', 'recorder', 'saxophone', 'singer', 'soloist', 'soprano', 'speaker', 'tenor', 'theorbo',
    'timpani', 'treble', 'trombone', 'trumpet', 'tuba', 'vibraphone', 'viola', 'violin', 'violoncello',
    'vocals', 'voice', 'xylophone'
)))
);
UPDATE album_info SET signature = NULL
WHERE album_library_id IN (SELECT o.album_library_id FROM occurrence o JOIN mapping_state ms
                           ON ms.library_track_id = o.library_track_id WHERE ms.file_fingerprint = 'reread:0009')
