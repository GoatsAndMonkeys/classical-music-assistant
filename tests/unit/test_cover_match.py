"""Tracklist matching for album covers (classical_core/cover_match.py)."""

import pytest

from classical_core import cover_match as cm
from tests.mbfake import uid

REL, REL2, REL3, RG, RG2 = uid(1), uid(2), uid(3), uid(11), uid(12)
LENGTHS = [412, 95, 288, 333, 610, 127]


@pytest.mark.parametrize(("name", "title"), [
    ("Bellini: I Puritani (Disc 2)", "I Puritani"),
    ("La Sonnambula (Sutherland, Pavarotti, Ghiaurov, Bonynge) CD 1", "La Sonnambula"),
    ("I Puritani - Vincenzo Bellini (Disc 1)", "I Puritani"),
    ("Arne: Artaxerxes=CD 2", "Artaxerxes"),
    ("Fidelio (Klemperer, disc 1)", "Fidelio"),
    ("Le Postillon De Lonjumeau [Adam]", "Le Postillon De Lonjumeau"),
    ("Bellini : I Puritani 2", "I Puritani"),
    ("01. La Sonnambula (Devia, Canonici)", "La Sonnambula"),
    ("Gustave III ou Le Bal Masqué (CD3) - Daniel-François-Esprit Auber", "Gustave III ou Le Bal Masqué"),
    ("Richard Wagner: Der Fliegende Holländer Disc 2", "Der Fliegende Holländer"),
])
def test_clean_title(name, title):
    assert cm.clean_title(name) == title


@pytest.mark.parametrize("name", [
    "240127 BSO Lady Macbeth", "0711 Mahler 6 Gergiev", "Tempest BBC", "130723 Prom 15 Walküre",
    "Ligeti: Violin Concerto — Royal Festival Hall, 2000-03-31", "RISURREZIONE- RAI Torino October 22, 1971",
    "I Puritani Edinburgh 8/9/60 (1)", "Adam - Le Toréador (2003.12.08 Paris)", "Norma 1971",
    "Gotterdammerung - Bayreuth - 2016", "Carnegie Hall Live", "Met",
])
def test_broadcasts_are_never_matched(name):
    assert cm.looks_broadcast(name)
    rip = cm.Rip(name, {1: LENGTHS}, {"callas"}, {"bellini"})
    assert not cm.wanted(rip)


def test_commercial_titles_are_not_broadcasts():
    for name in ("Bellini: I Puritani (Disc 2)", "La Sonnambula (Sutherland, Pavarotti, Ghiaurov, Bonynge) CD 1",
                 "Adès: The Tempest [Disc 1]", "Norma Disc 3"):
        assert not cm.looks_broadcast(name), name


def _tracks(lengths, disc=None, artist="Maria Callas; Giuseppe di Stefano"):
    return [(disc, i, d, artist) for i, d in enumerate(lengths, 1)]


def test_rip_from_tracks_needs_whole_discs():
    rip = cm.rip_from_tracks("I Puritani (Disc 2)", "Various Artists", _tracks(LENGTHS), ["Tullio Serafin"],
                             ["Vincenzo Bellini"])
    assert rip.discs == {1: LENGTHS}
    assert rip.performers == {"callas", "stefano", "serafin"}          # 'Various Artists' is nobody
    assert rip.composers == {"bellini"}
    assert cm.wanted(rip)
    # two rips filed under one album name (positions repeat), a partial rip, no positions: no rip
    assert cm.rip_from_tracks("x", None, _tracks(LENGTHS) + _tracks(LENGTHS), [], []) is None
    assert cm.rip_from_tracks("x", None, _tracks(LENGTHS)[1:], [], []) is None
    assert cm.rip_from_tracks("x", None, [(None, None, 100, "a")], [], []) is None
    # too few tracks, a missing length, a title that is no title
    assert not cm.wanted(cm.rip_from_tracks("I Puritani", None, _tracks(LENGTHS[:3]), [], []))
    assert not cm.wanted(cm.rip_from_tracks("I Puritani", None, _tracks([*LENGTHS[:5], None]), [], []))
    assert not cm.wanted(cm.rip_from_tracks("2", None, _tracks(LENGTHS), [], []))
    assert not cm.wanted(cm.rip_from_tracks("Unknown Album (2/11/2009)", None, _tracks(LENGTHS), [], []))


def test_name_keys_split_messy_credits():
    assert cm.name_keys(["Riccard MUTI, M.CABALLE; A.Kraus", "Orchestra del Teatro alla Scala di Milano"]) == \
        {"muti", "caballe", "kraus"}
    assert cm.name_keys(["Philharmonia Orchestra", "Various Artists"]) == set()


def _release(mbid, rg, media, credit=("Vincenzo Bellini", "Maria Callas")):
    return {"id": mbid, "title": "I puritani", "release-group": {"id": rg},
            "artist-credit": [{"name": n, "artist": {"name": n, "sort-name": n}} for n in credit],
            "media": [{"track-count": len(m), "tracks": [{"position": i, "length": None if x is None else x * 1000}
                                                         for i, x in enumerate(m, 1)]} for m in media]}


def _hit(mbid, rg, counts, score=100, title="I puritani", credit=("Vincenzo Bellini", "Maria Callas")):
    return {"id": mbid, "score": score, "title": title, "release-group": {"id": rg},
            "artist-credit": [{"name": n, "artist": {"name": n}} for n in credit],
            "media": [{"track-count": c} for c in counts]}


RIP = cm.Rip("Bellini: I Puritani (Disc 2)", {1: LENGTHS}, {"callas", "stefano"}, {"bellini"})


def test_confirms_by_lengths_and_people():
    other = [20, 20]
    assert cm.confirms(_release(REL, RG, [other, [x + 2 for x in LENGTHS]]), RIP)        # disc 2 of the set
    assert not cm.confirms(_release(REL, RG, [[x + 4 for x in LENGTHS]]), RIP)           # off by 4 s each
    off_one = [*LENGTHS[:5], LENGTHS[5] + 20]
    assert not cm.confirms(_release(REL, RG, [off_one]), RIP)                           # one track another take
    assert not cm.confirms(_release(REL, RG, [LENGTHS + [100]]), RIP)                   # a bonus track
    assert not cm.confirms(_release(REL, RG, [[None] * 6]), RIP)                         # MB knows no lengths
    # the timings alone are not enough: someone of ours must be credited
    assert not cm.confirms(_release(REL, RG, [LENGTHS], credit=("Joan Sutherland",)), RIP)
    assert cm.confirms(_release(REL, RG, [LENGTHS], credit=("Bellini",)), RIP)


def test_confirms_a_whole_set_disc_by_disc():
    rip = cm.Rip("I Puritani", {1: LENGTHS, 2: LENGTHS[::-1]}, {"callas"}, set())
    assert cm.confirms(_release(REL, RG, [LENGTHS, LENGTHS[::-1]]), rip)
    assert not cm.confirms(_release(REL, RG, [LENGTHS[::-1], LENGTHS]), rip)
    assert not cm.confirms(_release(REL, RG, [LENGTHS]), rip)


def test_candidates_filter_and_order():
    search = {"releases": [
        _hit(REL, RG, [17, 21], credit=("Joan Sutherland",)),
        _hit(REL2, RG, [20, 6], score=95),                       # credits Callas: first despite the score
        _hit(REL3, RG2, [6], score=60),                          # weak score
        _hit(uid(4), RG2, [9]),                                  # no medium of 6 tracks
        _hit(uid(5), RG2, [6], title="I puritani: highlights", credit=("Bellini",)),  # title differs in words (still contains them)
        _hit(uid(6), RG2, [6], title="Puritanical"),             # not the title's words
    ]}
    rip = cm.Rip("I Puritani", {1: LENGTHS}, {"callas"}, {"bellini"})
    rip17 = cm.Rip("I Puritani", {1: [100] * 17}, {"callas"}, {"bellini"})
    assert [h["id"] for h in cm.candidates(search, rip)] == [REL2, uid(5)]
    assert [h["id"] for h in cm.candidates(search, rip17)] == [REL]


def test_decide_one_release_one_group_or_nothing():
    good = _release(REL, RG, [LENGTHS])
    hits = [_hit(REL, RG, [6]), _hit(REL2, RG, [6]), _hit(REL3, RG2, [6], credit=("Joan Sutherland",))]
    # one confirmed release, the rest looked up and not ours: the release
    m = cm.decide(hits, {REL: good, REL2: _release(REL2, RG, [[1] * 6]), REL3: _release(REL3, RG2, [[1] * 6])}, RIP)
    assert m == cm.Match(REL, RG, (REL,))
    # two pressings of one group: the group, no release
    m = cm.decide(hits, {REL: good, REL2: _release(REL2, RG, [LENGTHS])}, RIP)
    assert m == cm.Match(None, RG, tuple(sorted((REL, REL2))))
    # an unchecked sibling in the group: the group only
    assert cm.decide(hits, {REL: good}, RIP) == cm.Match(None, RG, (REL,))
    # two groups confirmed: nothing
    assert cm.decide(hits, {REL: good, REL3: _release(REL3, RG2, [LENGTHS])}, RIP) is None
    # an unchecked rival crediting our performers in another group: nothing
    rival = [_hit(REL, RG, [6]), _hit(REL3, RG2, [6])]
    assert cm.decide(rival, {REL: good}, RIP) is None
    # nothing confirmed, or the confirmed release has no group: nothing
    assert cm.decide(hits, {REL2: _release(REL2, RG, [[1] * 6])}, RIP) is None
    nogroup = dict(good, **{"release-group": {}})
    assert cm.decide([_hit(REL, None, [6])], {REL: nogroup}, RIP) is None


def test_search_url_carries_title_and_track_count():
    url = cm.search_url("https://musicbrainz.org/ws/2", "I Puritani", 21)
    assert "release%3A%22I%20Puritani%22%20AND%20tracksmedium%3A21" in url and url.endswith("limit=100&fmt=json")
    assert "inc=recordings+artist-credits+release-groups" in cm.lookup_url("https://musicbrainz.org/ws/2", REL)
