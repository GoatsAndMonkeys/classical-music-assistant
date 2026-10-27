"""Name resolution (PLAN.md §5.11): parsing, the authority list, MusicBrainz artist search, review-only
clustering, the optional LLM fallback (against a local fake server) and user corrections."""

import asyncio
import json

import pytest

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.mb import MBClient
from classical_core.names import (
    LLMClient,
    NameContext,
    album_name_prefix,
    authority_match,
    compatible,
    given_name_conflict,
    parse_names,
    pick_mb_artist,
)
from classical_core.store import connect, migrate
from classical_core.tags import Person, TagInfo

BEETHOVEN_STRINGS = [
    "beethoven",
    "Ludwig van Beethoven",
    "Beethoven, Ludwig Van [Composer]/Ludwig van Beethoven/Sonnleithner, Joseph von [Author]/Ludwig Van Beethoven",
    "Ludwig Van Beethoven [1770-1827]/Ludwig Van Beethoven",
    "Ludwig van Beethoven/Ludwig Van Beethoven",
    "Beethoven, Ludwig van (1770-1827)",
]


# ---- (a) parsing ---------------------------------------------------------------------------------------


def test_parse_drops_author_dedupes_and_inverts():
    cs = parse_names(BEETHOVEN_STRINGS[2])
    assert [c.name for c in cs] == ["Ludwig van Beethoven"]
    c = parse_names("Beethoven, Ludwig van (1770-1827)")[0]
    assert (c.name, c.born, c.died) == ("Ludwig van Beethoven", 1770, 1827)
    c = parse_names("Ludwig Van Beethoven [1770-1827]")[0]
    assert (c.name, c.born, c.died) == ("Ludwig van Beethoven", 1770, 1827)
    assert [c.name for c in parse_names("Franz Schubert, arr. Richard Strauss")] == ["Franz Schubert"]
    assert [c.name for c in parse_names("Gilbert & Sullivan")] == ["Gilbert", "Sullivan"]
    assert [c.name for c in parse_names("Strauss II, Johann")] == ["Johann Strauss II"]
    assert [c.name for c in parse_names("Davis, Jr.")] == ["Davis, Jr."]


def test_last_first_with_dates_resolves_and_checks_dates():
    c = parse_names("Brahms, Johannes (1833-1897)")[0]
    assert authority_match(c)[0].name == "Johannes Brahms"
    wrong = parse_names("Brahms, Johannes (1733-1797)")[0]
    assert authority_match(wrong) == (None, "life dates disagree with the list")


# ---- (b) authority -------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", BEETHOVEN_STRINGS)
def test_six_beethoven_strings_resolve_to_one_composer(raw):
    cs = parse_names(raw)
    assert len(cs) == 1
    assert authority_match(cs[0])[0].name == "Ludwig van Beethoven"


def test_bach_and_strauss_abstain():
    for raw in ("Bach", "Strauss", "bach"):
        k, how = authority_match(parse_names(raw)[0])
        assert k is None and how == "ambiguous surname"


def test_world_ambiguous_surname_needs_album_context():
    c = parse_names("Mozart")[0]
    assert authority_match(c)[0] is None
    assert authority_match(c, NameContext(catalogue_surnames={"mozart"}))[0].name == "Wolfgang Amadeus Mozart"
    assert authority_match(c, NameContext(siblings={"Wolfgang Amadeus Mozart"}))[0] is not None


def test_alexander_prokofiev_is_a_review_proposal_not_a_merge():
    c = parse_names("Alexander Prokofiev")[0]
    assert authority_match(c)[0] is None
    assert given_name_conflict(c).name == "Sergei Prokofiev"


def test_compatible_is_conservative():
    assert compatible("J. S. Bach", "Johann Sebastian Bach")
    assert compatible("C. P. E. Bach", "Johann Sebastian Bach") is None
    assert compatible("Alexander Prokofiev", "Sergei Prokofiev") is None
    assert compatible("Brahms", "Johannes Brahms")
    assert compatible("Clara Schumann", "Robert Schumann") is None


def test_album_name_prefix():
    assert album_name_prefix("Beethoven/Fidelio") == "Beethoven"
    assert album_name_prefix("Beethoven: Fidelio Disc 1") == "Beethoven"
    assert album_name_prefix("Beethoven- Fidelio Disc 2") == "Beethoven"
    assert album_name_prefix("Beethoven : Fidelio1") == "Beethoven"
    assert album_name_prefix("Complete Beethoven Edition Vol.4-Opera-CD 3") is None


# ---- the index ------------------------------------------------------------------------------------------


class Reader:
    def __init__(self):
        self.tags: dict[str, TagInfo] = {}

    def __call__(self, path):
        return self.tags[path]


def make_index(tmp_path, mb=None, llm=None):
    con = connect(tmp_path / "index.db")
    migrate(con)
    r = Reader()
    return Index(con, mb, tag_reader=r, llm=llm), r


def add_album(idx, reader, aid, name, rows, artist="", force=False):
    """rows: (title, composer tag or None, work tag or None, credits)"""
    tracks = []
    for i, (title, comp, work, credits) in enumerate(rows):
        path = f"{aid}/{i}"
        tracks.append(TrackIn(f"{aid}-{i}", 1, i + 1, title, artist, 300,
                              mappings=[MappingIn("filesystem_local", path, "f", path)]))
        reader.tags[path] = TagInfo(title=title, artist=artist, composers=[comp] if comp else [], work=work,
                                    performers=list(credits or []))
    asyncio.run(idx.index_album(AlbumIn(aid, name, artist, 1990, None, tracks), force=force))


def composer_names(idx):
    return sorted(c["name"] for c in idx.composers())


def test_six_beethoven_spellings_are_one_composer_in_the_list(tmp_path):
    idx, r = make_index(tmp_path)
    for n, raw in enumerate(BEETHOVEN_STRINGS):
        add_album(idx, r, f"A{n}", f"Album {n}", [(f"Symphony No. {n + 1}: I. Allegro", raw, None, None),
                                                   (f"Symphony No. {n + 1}: II. Andante", raw, None, None)])
    assert composer_names(idx) == ["Ludwig van Beethoven"]


def test_bernstein_conducting_is_not_the_composer(tmp_path):
    """Real shape: DG Complete Beethoven Edition, TPE1 Leonard Bernstein, TALB 'Beethoven/Fidelio', no TCOM."""
    idx, r = make_index(tmp_path)
    lb = Person("Leonard Bernstein", "conductor", "person")
    add_album(idx, r, "B", "Beethoven/Fidelio",
              [("Quartett - Mir ist so wunderbar", None, None, [lb]), ("Arie - Hat man nicht auch Gold", None, None, [lb]),
               ("Marsch", None, None, [lb])], artist="Leonard Bernstein")
    assert "Leonard Bernstein" not in composer_names(idx)
    assert composer_names(idx) == ["Ludwig van Beethoven"]


def test_performing_composer_never_taken_from_the_artist_field(tmp_path):
    idx, r = make_index(tmp_path)
    add_album(idx, r, "B", "Live in Vienna", [("Symphony No. 5: I. Allegro", None, None, None),
                                              ("Symphony No. 5: II. Andante", None, None, None)],
              artist="Leonard Bernstein")
    assert "Leonard Bernstein" not in composer_names(idx)


def test_bach_stays_unresolved_and_goes_to_review(tmp_path):
    idx, r = make_index(tmp_path)
    add_album(idx, r, "A", "Cantatas", [("Cantata BWV 140: I. Wachet auf", "Bach", None, None),
                                        ("Cantata BWV 140: II. Er kommt", "Bach", None, None)])
    assert composer_names(idx) == ["Bach"]
    assert any(v["reason"].startswith("composer not resolved") for v in idx.review())


def test_raw_name_similar_to_existing_composer_is_proposed_not_merged(tmp_path):
    idx, r = make_index(tmp_path)
    add_album(idx, r, "A", "One", [("Partita No. 1: I. Allemande", "Heinrich Ignaz Franz Biber", None, None),
                                   ("Partita No. 1: II. Sarabande", "Heinrich Ignaz Franz Biber", None, None)])
    add_album(idx, r, "B", "Two", [("Partita No. 2: I. Allemande", "H. I. F. Biber", None, None),
                                   ("Partita No. 2: II. Sarabande", "H. I. F. Biber", None, None)])
    assert len(idx.composers()) == 2
    assert any(v["reason"] == "possible same composer" for v in idx.review())


def test_user_merge_applies_everywhere_and_survives_rederive(tmp_path):
    idx, r = make_index(tmp_path)
    rows_a = [("Partita No. 1: I. Allemande", "Heinrich Ignaz Franz Biber", None, None),
              ("Partita No. 1: II. Sarabande", "Heinrich Ignaz Franz Biber", None, None)]
    rows_b = [("Partita No. 2: I. Allemande", "H. I. F. Biber", None, None),
              ("Partita No. 2: II. Sarabande", "H. I. F. Biber", None, None)]
    add_album(idx, r, "A", "One", rows_a)
    add_album(idx, r, "B", "Two", rows_b)
    by_name = {c["name"]: c["id"] for c in idx.composers()}
    idx.merge_composers(by_name["H. I. F. Biber"], by_name["Heinrich Ignaz Franz Biber"], user="seth")
    assert composer_names(idx) == ["Heinrich Ignaz Franz Biber"]
    assert idx.composers()[0]["works"] == 2
    add_album(idx, r, "B", "Two", rows_b, force=True)
    assert composer_names(idx) == ["Heinrich Ignaz Franz Biber"]


def test_user_name_identity_wins_over_the_authority(tmp_path):
    idx, r = make_index(tmp_path)
    add_album(idx, r, "A", "One", [("Symphony No. 1: I. Allegro", "Leopold Mozart", None, None),
                                   ("Symphony No. 1: II. Andante", "Leopold Mozart", None, None)])
    add_album(idx, r, "B", "Two", [("Symphony No. 9: I. Allegro", "Mozart", None, None),
                                   ("Symphony No. 9: II. Andante", "Mozart", None, None)])
    leopold = next(c["id"] for c in idx.composers() if c["name"] == "Leopold Mozart")
    idx.set_name_identity("Mozart", leopold, user="seth")
    add_album(idx, r, "B", "Two", [("Symphony No. 9: I. Allegro", "Mozart", None, None),
                                   ("Symphony No. 9: II. Andante", "Mozart", None, None)])
    assert composer_names(idx) == ["Leopold Mozart"]
    assert idx.composers()[0]["works"] == 2


# ---- (c) MusicBrainz artist search ---------------------------------------------------------------------

BIBER_MBID = "0c2b2f9a-1d8c-4b5e-9b8e-3f1f6c0d2a11"


def artist_search_fetch(calls):
    async def fetch(url, headers):
        calls.append(url)
        if "/artist?" in url and "Biber" in url:
            return 200, json.dumps({"artists": [
                {"id": BIBER_MBID, "type": "Person", "score": 100, "name": "Heinrich Ignaz Franz Biber",
                 "sort-name": "Biber, Heinrich Ignaz Franz", "life-span": {"begin": "1644-08-12", "end": "1704-05-03"},
                 "aliases": [{"name": "Heinrich Ignaz Franz von Biber", "sort-name": "Biber, Heinrich Ignaz Franz von"},
                             {"name": "H. I. F. Biber", "sort-name": "Biber, H. I. F."}]},
                {"id": "11111111-1111-4111-8111-111111111111", "type": "Group", "score": 95, "name": "Biber Ensemble"},
            ]}).encode()
        return 200, b'{"artists": []}'

    return fetch


def test_pick_mb_artist_needs_name_match_and_dates():
    data = asyncio.run(artist_search_fetch([])("x/artist?query=Biber", {}))[1]
    c = parse_names("Biber, Heinrich Ignaz Franz von (1644-1704)")[0]
    art, _ = pick_mb_artist(c, json.loads(data))
    assert art and art.mbid == BIBER_MBID
    assert pick_mb_artist(parse_names("Heinrich Biber (1700-1750)")[0], json.loads(data))[0] is None
    assert pick_mb_artist(parse_names("Franz Biber")[0], json.loads(data))[0] is None   # no exact name/alias


def test_mb_search_unifies_spellings_by_mbid_and_is_cached_as_evidence(tmp_path):
    calls: list[str] = []
    mb = MBClient("https://example.invalid/contact", fetch=artist_search_fetch(calls), interval=0)
    idx, r = make_index(tmp_path, mb=mb)
    add_album(idx, r, "A", "One", [("Partita No. 1: I. Allemande", "Heinrich Ignaz Franz Biber", None, None),
                                   ("Partita No. 1: II. Sarabande", "Heinrich Ignaz Franz Biber", None, None)])
    add_album(idx, r, "B", "Two", [("Partita No. 2: I. Allemande", "Biber, H. I. F.", None, None),
                                   ("Partita No. 2: II. Sarabande", "Biber, H. I. F.", None, None)])
    assert composer_names(idx) == ["Heinrich Ignaz Franz Biber"]
    searches = [u for u in calls if "/artist?" in u]
    assert len(searches) == 2 and all("type%3Aperson" in u for u in searches)
    add_album(idx, r, "B", "Two", [("Partita No. 2: I. Allemande", "Biber, H. I. F.", None, None)], force=True)
    assert len([u for u in calls if "/artist?" in u]) == 2      # evidence: no second search


# ---- (e) optional LLM, against a local fake OpenAI-compatible server ------------------------------------


async def _with_fake_llm(answer, body_seen):
    from aiohttp import web

    async def completions(request):
        body = await request.json()
        body_seen.append((dict(request.headers), body))
        items = json.loads(body["messages"][1]["content"])["items"]
        results = [answer(it) for it in items]
        return web.json_response({"choices": [{"message": {"content": json.dumps({"results": results})}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


def test_llm_fallback_proposals(tmp_path):
    seen: list = []

    def answer(it):
        if "Beethofen" in it["name"]:
            return {"id": it["id"], "canonical": "Ludwig van Beethoven", "mbid": None, "confidence": 0.97}
        return {"id": it["id"], "canonical": "Someone Else", "mbid": None, "confidence": 0.6}

    async def run():
        runner, base = await _with_fake_llm(answer, seen)
        try:
            idx, r = make_index(tmp_path, llm=LLMClient(base, "test-model", api_key="sk-test"))
            tracks = []
            for aid, comp in (("A", "Ludvig van Beethofen"), ("B", "Johann Q. Nobody")):
                tracks = []
                for i in range(2):
                    path = f"{aid}/{i}"
                    tracks.append(TrackIn(f"{aid}{i}", 1, i + 1, f"Sonata No. 1: {'I' * (i + 1)}. Allegro", "", 1,
                                          mappings=[MappingIn("filesystem_local", path, "f", path)]))
                    r.tags[path] = TagInfo(title=tracks[-1].title, composers=[comp])
                await idx.index_album(AlbumIn(aid, aid, "", None, None, tracks))
            return idx
        finally:
            await runner.cleanup()

    idx = asyncio.run(run())
    names = composer_names(idx)
    assert "Ludwig van Beethoven" in names            # confident and confirmed by the authority list: applied
    assert "Johann Q. Nobody" in names                # low confidence: kept raw, proposal goes to review
    assert any(v["reason"] == "composer name proposed by the language model" for v in idx.review())
    headers, body = seen[0]
    assert headers["Authorization"] == "Bearer sk-test" and body["model"] == "test-model"
    assert body["temperature"] == 0
    sent = json.loads(body["messages"][1]["content"])["items"]
    assert set(sent[0]) == {"id", "name", "album", "tracks"}


def test_llm_bad_reply_is_ignored():
    async def fetch(url, headers, body):
        return 200, json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode()

    client = LLMClient("http://x", "m", fetch=fetch)
    assert asyncio.run(client.resolve([{"id": "a", "name": "x", "album": "", "tracks": []}])) == {}


def test_initials_resolve_only_when_one_bearer_fits():
    assert authority_match(parse_names("Strauss, R.")[0])[0].name == "Richard Strauss"
    assert authority_match(parse_names("W. A. Mozart")[0])[0].name == "Wolfgang Amadeus Mozart"
    assert authority_match(parse_names("J. Bach")[0])[0] is None
    assert authority_match(parse_names("Strauss, J.")[0])[0] is None


def test_conflicting_life_dates_are_never_folded_into_the_listed_composer(tmp_path):
    """Mozart's son was billed 'Wolfgang Amadeus Mozart'; his dates (1791-1844) rule out the listed W. A."""
    idx, r = make_index(tmp_path)
    add_album(idx, r, "A", "Son", [("Piano Concerto No. 1: I. Allegro", "Wolfgang Amadeus Mozart (1791-1844)", None, None),
                                   ("Piano Concerto No. 1: II. Andante", "Wolfgang Amadeus Mozart (1791-1844)", None, None)])
    add_album(idx, r, "B", "Father", [("Symphony No. 40: I. Allegro", "Wolfgang Amadeus Mozart", None, None),
                                      ("Symphony No. 40: II. Andante", "Wolfgang Amadeus Mozart", None, None)])
    add_album(idx, r, "C", "Son again", [("Piano Concerto No. 2: I. Allegro", "Mozart, Wolfgang Amadeus (1791-1844)",
                                          None, None),
                                         ("Piano Concerto No. 2: II. Rondo", "Mozart, Wolfgang Amadeus (1791-1844)",
                                          None, None)])
    cs = sorted((c["born"] or "", c["works"]) for c in idx.composers())
    assert cs == [("1756", 1), ("1791", 2)]
    assert any("life dates disagree" in v["reason"] for v in idx.review())


def test_same_name_with_different_dates_on_one_album_is_not_memoised_as_one(tmp_path):
    idx, r = make_index(tmp_path)
    add_album(idx, r, "A", "Family", [("Piano Concerto No. 1: I. Allegro", "Wolfgang Amadeus Mozart (1791-1844)", None,
                                       None),
                                      ("Piano Concerto No. 1: II. Andante", "Wolfgang Amadeus Mozart (1791-1844)", None,
                                       None),
                                      ("Symphony No. 40: I. Allegro", "Wolfgang Amadeus Mozart (1756-1791)", None, None),
                                      ("Symphony No. 40: II. Andante", "Wolfgang Amadeus Mozart (1756-1791)", None, None)])
    assert len(idx.composers()) == 2
