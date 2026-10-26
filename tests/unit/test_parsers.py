from classical_core.catalogue import catalogues_conflict, parse_catalogues, parse_key, work_type
from classical_core.composers import find_in_credit, lookup
from classical_core.titles import parse_title, shared_prefix_runs


def keys(text):
    return [c.key for c in parse_catalogues(text)]


def test_compound_catalogues_are_lossless():
    assert keys("Prelude in E minor, Op. 28 No. 4") == ["op:28/4"]
    assert keys("Prelude, op 28, no. 7") == ["op:28/7"]
    assert keys("Symphony No. 9 in E minor, Op. 95, B. 178") == ["op:95", "b:178"]
    assert keys("Piano Sonata No. 11, K. 331/300i")[0] == "k:331/300i"
    assert parse_catalogues("Trumpet Concerto, Hob. VIIe:1")[0].display == "Hob. VIIe:1"
    assert keys("Bach's 2 suites") == []
    a, b = parse_catalogues("Op. 28")[0], parse_catalogues("Op. 28 No. 4")[0]
    assert a != b and a.is_prefix_of(b)
    assert catalogues_conflict(parse_catalogues("Op. 28 No. 4"), parse_catalogues("Op. 28 No. 7"))
    assert not catalogues_conflict(parse_catalogues("Op. 95"), parse_catalogues("B. 178"))


def test_keys_and_types():
    assert parse_key("Symphony No. 5 in C minor") == "C minor"
    assert parse_key("Concerto in E flat major") == "E-flat major"
    assert parse_key("Symphonie Nr. 1 C-dur") == "C major"
    assert work_type("Symphony No. 5 in C minor, Op. 67") == "Symphonies"
    assert work_type("24 Preludes, Op. 28") == "Keyboard"
    assert work_type("Le nozze di Figaro, K. 492", None, ["Overture", "Act I: No. 1 Duettino"]) == "Opera & Stage"
    assert work_type("Whatever", "Symphony") == "Symphonies"


def test_title_splits():
    p = parse_title("Symphony No. 5 in C minor, Op. 67: III. Allegro")
    assert (p.work, p.movement_no, p.movement_name) == ("Symphony No. 5 in C minor, Op. 67", 3, "Allegro")
    p = parse_title("Symphony No. 9 - 2. Largo")
    assert (p.work, p.movement_no, p.movement_name) == ("Symphony No. 9", 2, "Largo")
    p = parse_title("Haydn: Trumpet Concerto in E flat, Hob. VIIe:1")
    assert p.composer_hint == "Haydn" and p.work.startswith("Trumpet Concerto") and p.movement_no is None
    assert parse_title("Canon in D") is None
    assert parse_title("Dancing in the Static") is None


def test_shared_prefix_localized():
    titles = ["Symphonie Nr. 1 C-dur, op. 21, 1. Adagio molto", "Symphonie Nr. 1 C-dur, op. 21, 2. Andante",
              "Symphonie Nr. 1 C-dur, op. 21, 3. Menuetto", "Egmont-Ouvertüre, op. 84"]
    assert shared_prefix_runs(titles) == [(0, 3, "Symphonie Nr. 1 C-dur, op. 21")]
    # decreasing numbers are not a run
    assert shared_prefix_runs(["Suite, 2. A", "Suite, 1. B"]) == []


def test_composer_authority_is_exact():
    assert lookup("Johannes Brahms").name == "Johannes Brahms"
    assert lookup("Antonin Dvorak").name == "Antonín Dvořák"
    assert lookup("Bach") is None and lookup("Strauss") is None
    assert find_in_credit("Johannes Brahms; Wiener Philharmoniker").name == "Johannes Brahms"
    assert find_in_credit("The Synths") is None
