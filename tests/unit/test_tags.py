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


def test_a_part_sung_is_not_the_instrument():
    """'Martha Mödl (Gutrune)': the parenthesis names her part; she sings it."""
    from classical_core.tags import _person
    for text, want in [("Hans Hotter (Wotan)", ("Hans Hotter", "voice", "Wotan")),
                       ("Kim Begley (2nd Norn)", ("Kim Begley", "voice", "2nd Norn")),
                       ("X (Voice of Apollo)", ("X", "voice", "Voice of Apollo")),
                       ("Y (Roberto, Earl of Leicester)", ("Y", "voice", "Roberto, Earl of Leicester")),
                       ("Ian Bostridge (tenor, Evangelist)", ("Ian Bostridge", "tenor", "Evangelist")),
                       ("Z (Music Master)", ("Z", "voice", "Music Master"))]:
        p = _person(text, "performer")
        assert (p.name, p.role, p.detail) == want, text
    for text, role in [("A (principal viola)", "principal viola"), ("B (intermission interviewer)",
                       "intermission interviewer"), ("C (tenor soloist)", "tenor soloist"),
                       ("D (viola da gamba)", "viola da gamba"), ("E (Leader of the Players / host)",
                       "leader of the players / host")]:
        p = _person(text, "performer")
        assert (p.role, p.detail) == (role, None), text


def test_a_semicolon_inside_a_parenthesis_does_not_split_the_credit():
    from classical_core.tags import parse
    info = parse({"performer": ["Martha Mödl (Gutrune; 3rd Norn); Hans Hotter (Wotan)"]})
    assert [(p.name, p.role, p.detail) for p in info.performers] == [
        ("Martha Mödl", "voice", "Gutrune; 3rd Norn"), ("Hans Hotter", "voice", "Wotan")]


def test_only_a_capitalised_name_is_a_part():
    """Review findings: an instrument missing from the vocabulary stays a role; a part named with a role
    word ('Norn I', 'A Boy', 'The Composer') is still a part."""
    from classical_core.tags import _person
    for text, want in [("X (erhu)", ("erhu", None)), ("X (Erhu)", ("erhu", None)), ("X (cimbalom)", ("cimbalom", None)),
                       ("X (Composer)", ("composer", None)), ("X (Violin I)", ("violin i", None)),
                       ("X (Norn I)", ("voice", "Norn I")), ("X (A Boy)", ("voice", "A Boy")),
                       ("X (The Composer)", ("voice", "The Composer")), ("X (tenor, Norn I)", ("tenor", "Norn I")),
                       ("X (tenor, evangelist)", ("tenor, evangelist", None)), ("X (Santoor)", ("santoor", None)),
                       ("X (SANTUR)", ("santur", None)), ("X (2ND NORN)", ("voice", "2ND NORN")),
                       ("X (Leader of the Players)", ("leader of the players", None)),
                       ("X (Voice of Apollo)", ("voice", "Voice of Apollo"))]:
        p = _person(text, "performer")
        assert (p.role, p.detail) == want, text


def test_an_unclosed_parenthesis_does_not_swallow_the_next_performer():
    from classical_core.tags import split_values
    assert split_values("A (Wotan; B (violin)") == ["A (Wotan", "B (violin)"]
    assert split_values("A (x\x00y); C") == ["A (x", "y)", "C"]


def test_a_tmcl_credit_names_an_instrument():
    """ID3 TMCL pairs a name with an instrument, so its role is never read as a part."""
    from classical_core.tags import _INSTRUMENT, parse
    info = parse({"performer": [f"{_INSTRUMENT}A (Erhu)", "B (Gutrune)"]})
    assert [(p.name, p.role, p.detail) for p in info.performers] == [("A", "erhu", None), ("B", "voice", "Gutrune")]


def test_files_with_credits_read_the_old_way_are_read_again(tmp_path):
    """Migration 0009 clears the fingerprint of a file whose stored credits hold a part as the role or a
    credit split inside its parenthesis, so the next sync reads it again; plain credits are left alone."""
    import json

    from classical_core.store import connect, migrate
    con = connect(tmp_path / "i.db")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '8')")
    from classical_core import store
    for num, sql in store._migrations():
        if num <= 8:
            for stmt in store._split_sql(sql):
                con.execute(stmt)
    credits = {"wotan": [{"name": "Hans Hotter", "role": "wotan", "kind": "person"}],
               "split": [{"name": "Martha Mödl (Gutrune", "role": "performer", "kind": "person"},
                         {"name": "3rd Norn)", "role": "performer", "kind": "person"}],
               "plain": [{"name": "A", "role": "viola", "kind": "person"}, {"name": "B", "role": "conductor",
                         "kind": "person"}, {"name": "Ain Anger (bass (Pater Profundus))", "role": "performer",
                         "kind": "person"}, {"name": "LSO", "role": "orchestra", "kind": "group"},
                         {"name": "C", "role": "voice", "kind": "person", "detail": "Gutrune"}]}
    for item, people in credits.items():
        con.execute("INSERT INTO album_info(album_library_id, signature) VALUES (?, 'sig')", (f"al-{item}",))
        con.execute("INSERT INTO occurrence(id, album_library_id, library_track_id) VALUES (?, ?, ?)",
                    (f"o-{item}", f"al-{item}", item))
        con.execute("INSERT INTO mapping_state(provider_instance, item_id, library_track_id, file_fingerprint) "
                    "VALUES ('fs', ?, ?, 'fp')", (f"d/{item}.flac", item))
        for i, v in enumerate(people):
            con.execute("INSERT INTO evidence(entity, entity_id, field, value, source, source_ref, edge, observed_at)"
                        " VALUES ('mapping', ?, 'performers', ?, 'tags', 'x', ?, 0)",
                        (f"fs/d/{item}.flac", json.dumps(v), f"performers:{i}"))
    con.commit()
    assert migrate(con) >= 9
    fps = dict(con.execute("SELECT library_track_id, file_fingerprint FROM mapping_state").fetchall())
    assert fps == {"wotan": "reread:0009", "split": "reread:0009", "plain": "fp"}
    sigs = dict(con.execute("SELECT album_library_id, signature FROM album_info").fetchall())
    assert sigs == {"al-wotan": None, "al-split": None, "al-plain": "sig"}   # so the sync plans them at all
