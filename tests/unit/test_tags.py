from classical_core.tags import read_raw, read_tags


def test_flac_full_picard(library):
    t = read_tags(library / "karajan_5_7" / "02.flac")
    assert t.work == "Symphony No. 5 in C minor, Op. 67"
    assert (t.movement_no, t.movement_total, t.movement_name) == (2, 4, "Andante con moto")
    assert t.composers == ["Ludwig van Beethoven"]
    assert t.recorded == "1962" and t.release_date == "2002"
    assert [(p.name, p.role, p.kind) for p in t.performers] == [
        ("Herbert von Karajan", "conductor", "person"), ("Berliner Philharmoniker", "orchestra", "group")]


def test_mp3_movement_frames(library):
    t = read_tags(library / "kleiber_5" / "03.mp3")
    assert t.work == "Symphony No. 5 in C minor, Op. 67"  # TIT1 counts as work because MVNM/MVIN are present
    assert (t.movement_no, t.movement_total, t.movement_name) == (3, 4, "Scherzo. Allegro")
    assert t.recorded == "1974-03"
    assert ("Carlos Kleiber", "conductor") in [(p.name, p.role) for p in t.performers]


def test_mp4_atoms(library):
    t = read_tags(library / "marriner_40" / "01.m4a")
    assert t.work == "Symphony No. 40 in G minor, K. 550"
    assert (t.movement_no, t.movement_total, t.movement_name) == (1, 4, "Molto allegro")
    assert t.recorded is None and t.release_date == "2010"
    assert [p.name for p in t.performers if p.role == "conductor"] == ["Neville Marriner"]


def test_tit1_without_movement_frames_is_grouping(library):
    raw = read_raw(library / "compilation" / "02.mp3")
    assert "work" not in raw and raw["grouping"] == ["Adagio for Strings, Op. 11"]


def test_recording_mbid_and_hash(library):
    t = read_tags(library / "kertesz_9" / "01.flac")
    assert t.recording_mbid == "e26123d1-0cc0-4394-9e95-c1ea14a7e826" and not t.has_classical_tags
    assert t.tag_hash() == read_tags(library / "kertesz_9" / "01.flac").tag_hash()
    assert t.tag_hash() != read_tags(library / "kertesz_9" / "02.flac").tag_hash()
