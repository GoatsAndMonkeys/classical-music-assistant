"""Cross-album work identity for works no MusicBrainz id ties together (DEVIATIONS.md, "Cross-album works").

Two albums' title-derived works are the same work only on strong evidence, each under the same resolved
composer:

* a bundled list of distinctive works (operas and other named stage, choral and orchestral works) whose
  normalized name the title matches exactly ('Fidelio', 'FIDELIO', 'Fidelio, opera, Op. 72'), or
* the same full catalogue number ('Op. 72a' == 'op.72a', never 'Op. 72' or 'Op. 72b'; 'Op. 59 No. 1' is not
  'Op. 59 No. 2') *and* titles that agree on everything else they say: key, form, numbers outside the
  catalogue and at least one distinctive word ('Leonore Overture #3, Op. 72A' == 'Leonoren Overture Nr 3,
  op. 72a'). A same-number pair whose titles disagree is a review item, never a merge.

Generic titles ('Violin Concerto', 'Piano Concerto No. 3') carry neither and never merge. A false merge is
worse than a miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import catalogue as cat
from .composers import lookup
from .text import fold, norm_key
from .titles import split_act_suffix, strip_track_number

# ---- the known-work list ------------------------------------------------------------------------------
# composer (as composers._DATA names him) -> (entry id, work type, catalogue key or None, names...).
# Only names that are distinctive for that composer: a bare form ('Requiem', 'Symphony') is never listed.
# Revisions with their own name stay their own entry (Beethoven's 'Leonore' of 1805 is not 'Fidelio').

_STAGE = "Opera & Stage"
_CHORAL = "Choral & Sacred"
_ORCH = "Orchestral"
_KNOWN: dict[str, list[tuple[str, str, str | None, tuple[str, ...]]]] = {
    "Ludwig van Beethoven": [
        ("fidelio", _STAGE, "op:72", ("Fidelio",)),
        ("leonore", _STAGE, None, ("Leonore", "Leonore oder Der Triumph der ehelichen Liebe")),
        ("missa-solemnis", _CHORAL, "op:123", ("Missa solemnis",)),
        ("egmont", _STAGE, "op:84", ("Egmont",)),
        ("christus-olberge", _CHORAL, "op:85", ("Christus am Ölberge", "Christ on the Mount of Olives")),
        ("prometheus", _STAGE, "op:43", ("Die Geschöpfe des Prometheus", "The Creatures of Prometheus")),
    ],
    "Richard Wagner": [
        ("hollander", _STAGE, "wwv:63", ("Der fliegende Holländer", "The Flying Dutchman",
                                          "Le vaisseau fantôme")),
        ("tannhauser", _STAGE, "wwv:70", ("Tannhäuser", "Tannhäuser und der Sängerkrieg auf Wartburg")),
        ("lohengrin", _STAGE, "wwv:75", ("Lohengrin",)),
        ("tristan", _STAGE, "wwv:90", ("Tristan und Isolde", "Tristan and Isolde", "Tristan & Isolde")),
        ("meistersinger", _STAGE, "wwv:96", ("Die Meistersinger von Nürnberg", "The Mastersingers of Nuremberg")),
        ("rheingold", _STAGE, "wwv:86a", ("Das Rheingold", "The Rhinegold")),
        ("walkure", _STAGE, "wwv:86b", ("Die Walküre", "The Valkyrie")),
        ("siegfried", _STAGE, "wwv:86c", ("Siegfried",)),
        ("gotterdammerung", _STAGE, "wwv:86d", ("Götterdämmerung", "Twilight of the Gods")),
        ("parsifal", _STAGE, "wwv:111", ("Parsifal",)),
        ("rienzi", _STAGE, "wwv:49", ("Rienzi", "Rienzi, der Letzte der Tribunen")),
        ("siegfried-idyll", _ORCH, "wwv:103", ("Siegfried Idyll", "Siegfried-Idyll")),
        ("wesendonck", "Vocal", "wwv:91", ("Wesendonck Lieder", "Wesendonck-Lieder")),
    ],
    "Wolfgang Amadeus Mozart": [
        ("figaro", _STAGE, "k:492", ("Le nozze di Figaro", "The Marriage of Figaro", "Die Hochzeit des Figaro")),
        ("giovanni", _STAGE, "k:527", ("Don Giovanni",)),
        ("cosi", _STAGE, "k:588", ("Così fan tutte", "Cosi fan tutte")),
        ("zauberflote", _STAGE, "k:620", ("Die Zauberflöte", "The Magic Flute")),
        ("entfuhrung", _STAGE, "k:384", ("Die Entführung aus dem Serail", "The Abduction from the Seraglio")),
        ("idomeneo", _STAGE, "k:366", ("Idomeneo", "Idomeneo, re di Creta")),
        ("tito", _STAGE, "k:621", ("La clemenza di Tito",)),
    ],
    "Giuseppe Verdi": [
        ("aida", _STAGE, None, ("Aida", "Aïda")), ("traviata", _STAGE, None, ("La traviata",)),
        ("rigoletto", _STAGE, None, ("Rigoletto",)), ("trovatore", _STAGE, None, ("Il trovatore",)),
        ("otello", _STAGE, None, ("Otello",)), ("falstaff", _STAGE, None, ("Falstaff",)),
        ("don-carlo", _STAGE, None, ("Don Carlo", "Don Carlos")), ("nabucco", _STAGE, None, ("Nabucco",)),
        ("macbeth", _STAGE, None, ("Macbeth",)), ("ballo", _STAGE, None, ("Un ballo in maschera",)),
        ("forza", _STAGE, None, ("La forza del destino",)), ("boccanegra", _STAGE, None, ("Simon Boccanegra",)),
    ],
    "Giacomo Puccini": [
        ("boheme", _STAGE, None, ("La bohème", "La boheme")), ("tosca", _STAGE, None, ("Tosca",)),
        ("butterfly", _STAGE, None, ("Madama Butterfly", "Madame Butterfly")),
        ("turandot", _STAGE, None, ("Turandot",)), ("manon-lescaut", _STAGE, None, ("Manon Lescaut",)),
        ("fanciulla", _STAGE, None, ("La fanciulla del West",)), ("trittico", _STAGE, None, ("Il trittico",)),
        ("gianni-schicchi", _STAGE, None, ("Gianni Schicchi",)),
    ],
    "Richard Strauss": [
        ("salome", _STAGE, "op:54", ("Salome",)), ("elektra", _STAGE, "op:58", ("Elektra",)),
        ("rosenkavalier", _STAGE, "op:59", ("Der Rosenkavalier",)),
        ("ariadne", _STAGE, "op:60", ("Ariadne auf Naxos",)),
        ("frau-ohne-schatten", _STAGE, "op:65", ("Die Frau ohne Schatten",)),
        ("arabella", _STAGE, "op:79", ("Arabella",)), ("capriccio", _STAGE, "op:85", ("Capriccio",)),
        ("zarathustra", _ORCH, "op:30", ("Also sprach Zarathustra",)),
        ("heldenleben", _ORCH, "op:40", ("Ein Heldenleben",)),
        ("till", _ORCH, "op:28", ("Till Eulenspiegels lustige Streiche",)),
        ("don-juan", _ORCH, "op:20", ("Don Juan",)), ("don-quixote", _ORCH, "op:35", ("Don Quixote",)),
        ("alpensinfonie", "Symphonies", "op:64", ("Eine Alpensinfonie", "An Alpine Symphony")),
        ("tod-verklarung", _ORCH, "op:24", ("Tod und Verklärung", "Death and Transfiguration")),
        ("metamorphosen", _ORCH, None, ("Metamorphosen",)),
        ("vier-letzte-lieder", "Vocal", None, ("Vier letzte Lieder", "Four Last Songs")),
    ],
    "Georges Bizet": [("carmen", _STAGE, None, ("Carmen",)),
                      ("pecheurs", _STAGE, None, ("Les pêcheurs de perles", "The Pearl Fishers"))],
    "Claude Debussy": [("pelleas", _STAGE, "l:88", ("Pelléas et Mélisande",)),
                       ("la-mer", _ORCH, "l:109", ("La mer",))],
    "Béla Bartók": [("bluebeard", _STAGE, "sz:48", ("Bluebeard's Castle", "Duke Bluebeard's Castle",
                                                     "A kékszakállú herceg vára")),
                    ("mandarin", _STAGE, "sz:73", ("The Miraculous Mandarin",))],
    "Sergei Prokofiev": [("nevsky", _CHORAL, "op:78", ("Alexander Nevsky",)),
                         ("romeo-juliet", _STAGE, "op:64", ("Romeo and Juliet",)),
                         ("peter-wolf", _ORCH, "op:67", ("Peter and the Wolf",))],
    "Igor Stravinsky": [("sacre", _STAGE, None, ("The Rite of Spring", "Le sacre du printemps")),
                        ("firebird", _STAGE, None, ("The Firebird", "L'oiseau de feu")),
                        ("petrushka", _STAGE, None, ("Petrushka", "Petrouchka")),
                        ("psalms", _CHORAL, None, ("Symphony of Psalms",)),
                        ("rake", _STAGE, None, ("The Rake's Progress",)),
                        ("oedipus", _STAGE, None, ("Oedipus Rex",))],
    "Benjamin Britten": [("war-requiem", _CHORAL, "op:66", ("War Requiem",)),
                         ("peter-grimes", _STAGE, "op:33", ("Peter Grimes",)),
                         ("turn-screw", _STAGE, "op:54", ("The Turn of the Screw",)),
                         ("billy-budd", _STAGE, "op:50", ("Billy Budd",))],
    "George Frideric Handel": [("messiah", _CHORAL, "hwv:56", ("Messiah", "The Messiah")),
                               ("giulio-cesare", _STAGE, "hwv:17", ("Giulio Cesare", "Giulio Cesare in Egitto"))],
    "Joseph Haydn": [("creation", _CHORAL, None, ("Die Schöpfung", "The Creation")),
                     ("seasons", _CHORAL, None, ("Die Jahreszeiten", "The Seasons"))],
    "Johann Sebastian Bach": [("matthew", _CHORAL, "bwv:244", ("Matthäus-Passion", "St Matthew Passion",
                                                              "St. Matthew Passion", "Matthäuspassion")),
                              ("john", _CHORAL, "bwv:245", ("Johannes-Passion", "St John Passion",
                                                           "St. John Passion", "Johannespassion")),
                              ("goldberg", "Keyboard", "bwv:988", ("Goldberg Variations",))],
    "Felix Mendelssohn": [("elijah", _CHORAL, "op:70", ("Elias", "Elijah"))],
    "Gustav Mahler": [("lied-erde", "Vocal", None, ("Das Lied von der Erde",)),
                      ("wunderhorn", "Vocal", None, ("Des Knaben Wunderhorn",)),
                      ("kindertotenlieder", "Vocal", None, ("Kindertotenlieder",))],
    "Hector Berlioz": [("fantastique", "Symphonies", "op:14", ("Symphonie fantastique",)),
                       ("damnation", _STAGE, "op:24", ("La damnation de Faust",)),
                       ("troyens", _STAGE, None, ("Les Troyens",)),
                       ("romeo-berlioz", "Symphonies", "op:17", ("Roméo et Juliette",))],
    "Modest Mussorgsky": [("boris", _STAGE, None, ("Boris Godunov",)),
                          ("pictures", "Keyboard", None, ("Pictures at an Exhibition",))],
    "Pyotr Ilyich Tchaikovsky": [("onegin", _STAGE, "op:24", ("Eugene Onegin",)),
                                 ("swan-lake", _STAGE, "op:20", ("Swan Lake",)),
                                 ("nutcracker", _STAGE, "op:71", ("The Nutcracker",)),
                                 ("sleeping-beauty", _STAGE, "op:66", ("The Sleeping Beauty",)),
                                 ("pique-dame", _STAGE, "op:68", ("The Queen of Spades", "Pique Dame"))],
    "Gioachino Rossini": [("barbiere", _STAGE, None, ("Il barbiere di Siviglia", "The Barber of Seville")),
                          ("tell", _STAGE, None, ("Guillaume Tell", "William Tell")),
                          ("cenerentola", _STAGE, None, ("La Cenerentola",))],
    "Arnold Schoenberg": [("gurrelieder", _CHORAL, None, ("Gurre-Lieder", "Gurrelieder")),
                          ("pierrot", "Vocal", "op:21", ("Pierrot lunaire",)),
                          ("verklarte", "Chamber", "op:4", ("Verklärte Nacht",))],
    "Alban Berg": [("wozzeck", _STAGE, None, ("Wozzeck",)), ("lulu", _STAGE, None, ("Lulu",))],
    "Leoš Janáček": [("jenufa", _STAGE, None, ("Jenůfa", "Jenufa")),
                     ("glagolitic", _CHORAL, None, ("Glagolitic Mass",))],
    "Engelbert Humperdinck": [("hansel", _STAGE, None, ("Hänsel und Gretel", "Hansel and Gretel"))],
    "Carl Maria von Weber": [("freischutz", _STAGE, None, ("Der Freischütz",))],
    "Christoph Willibald Gluck": [("orfeo", _STAGE, None, ("Orfeo ed Euridice", "Orphée et Eurydice"))],
    "Claudio Monteverdi": [("orfeo-monteverdi", _STAGE, None, ("L'Orfeo",)),
                           ("poppea", _STAGE, None, ("L'incoronazione di Poppea",))],
    "Henry Purcell": [("dido", _STAGE, None, ("Dido and Aeneas",))],
}


_PAREN_KEEP = re.compile(r"\b(?:suites?|overtures?|ouvertures?|preludes?|vorspiel|excerpts?|highlights|auszuge|"
                         r"selections?|scenes?|fantas(?:y|ia|ie)|variations?|paraphrase|potpourri|dances?|"
                         r"intermezzo|interlude|music from|concert|arias?)\b")


def core_name(title: str | None) -> str:
    """'Der fliegende Holländer (The Flying Dutchman), opera, WWV 63' -> 'der fliegende hollander';
    'FIDELIO' -> 'fidelio'. The work's own name with its catalogue, genre label and parentheses removed."""
    s = fold(strip_track_number(title or ""))
    s = cat.strip_catalogues(s)
    # a translation or a label in brackets goes; one that names a different piece stays ('The Firebird
    # (1919 Suite)', 'Carmen (Suite)', 'Fidelio [Excerpts]' are not the stage work itself)
    s = re.sub(r"\(([^()]*)\)|\[([^\]]*)\]", lambda m: f" {m.group(0)[1:-1]} " if _PAREN_KEEP.search(
        m.group(0)) else " ", s)
    s = re.sub(r"[\"'“”‘’«»]", "", s)
    s = re.sub(r"\b(?:(?:grand|comic|romantic|romantische|komische)\s+)?(?:opera|oper|opéra|singspiel|"
               r"music drama|musikdrama|dramma giocoso|dramma per musica|opera (?:seria|buffa)|oratorio|ballet)\b"
               r"(?:\s+in\s+(?:\w+|\d+)\s+(?:acts?|akten|aufzugen|atti|actes))?", " ", s)
    s = re.sub(r"\bin\s+(?:one|two|three|four|five|\d)\s+(?:acts?|akten|aufzugen|atti|actes)\b", " ", s)
    return norm_key(s)


_KNOWN_INDEX: dict[tuple[str, str], tuple[str, str, str | None]] = {}
for _comp, _entries in _KNOWN.items():
    for _eid, _type, _cat, _names in _entries:
        for _n in _names:
            _k = lookup(_comp)
            if _k is None:          # every listed composer is in composers._DATA (a test checks it)
                continue
            _KNOWN_INDEX[(norm_key(_k.name), core_name(_n))] = (_eid, _type, _cat)


def known_names(composer: str | None) -> list[str]:
    """The listed works' names of a composer, as norm_key text ('alexander nevsky')."""
    k = lookup(composer)
    if k is None:
        return []
    return [norm_key(n) for _eid, _t, _c, names in _KNOWN.get(k.name, []) for n in names]


def known_work(composer: str | None, title: str | None) -> tuple[str, str] | None:
    """(entry id, work type) when the title names a listed work of this composer exactly ('Fidelio, Op. 72',
    'FIDELIO'). A title whose own catalogue number contradicts the entry's is not that work."""
    k = lookup(composer)
    if k is None or not title:
        return None
    hit = _KNOWN_INDEX.get((norm_key(k.name), core_name(title)))
    if hit is None:
        return None
    eid, wtype, ckey = hit
    if ckey:
        ns = ckey.split(":", 1)[0]
        mine = [c for c in cat.parse_catalogues(title) if c.namespace == ns]
        if mine and mine[0].key != ckey:
            return None
    return eid, wtype


# ---- same catalogue number: do the titles agree on everything else? ----------------------------------

_FORMS = {
    "overture": "overture", "ouverture": "overture", "ouvertüre": "overture",
    "symphony": "symphony", "symphonie": "symphony", "sinfonie": "symphony", "sinfonia": "symphony",
    "concerto": "concerto", "konzert": "concerto", "concert": "concerto",
    "sonata": "sonata", "sonate": "sonata", "sonatina": "sonatina", "quartet": "quartet", "quartett": "quartet",
    "quatuor": "quartet", "quartetto": "quartet", "quintet": "quintet", "quintett": "quintet",
    "trio": "trio", "sextet": "sextet", "septet": "septet", "octet": "octet", "mass": "mass", "messe": "mass",
    "missa": "mass", "requiem": "requiem", "suite": "suite", "variations": "variations",
    "variationen": "variations", "prelude": "prelude", "vorspiel": "prelude", "preludio": "prelude",
    "fugue": "fugue", "fuge": "fugue", "serenade": "serenade", "rhapsody": "rhapsody", "march": "march",
    "marsch": "march", "dance": "dance", "dances": "dance", "fantasia": "fantasia", "fantasy": "fantasia",
    "fantasie": "fantasia", "nocturne": "nocturne", "etude": "etude", "ballade": "ballade", "scherzo": "scherzo",
    "impromptu": "impromptu", "cantata": "cantata", "kantate": "cantata", "aria": "aria", "arie": "aria",
    "song": "song", "lied": "song", "lieder": "song", "intermezzo": "intermezzo", "rondo": "rondo",
    "finale": "finale", "act": "act", "akt": "act", "aufzug": "act", "scene": "scene", "szene": "scene",
    "excerpts": "excerpts", "highlights": "excerpts", "auszuge": "excerpts", "suites": "suite",
}
_SCORING = {"piano", "klavier", "violin", "violine", "viola", "cello", "violoncello", "flute", "oboe",
            "clarinet", "klarinette", "bassoon", "horn", "trumpet", "organ", "orgel", "harp", "guitar", "strings",
            "string", "orchestra", "orchester", "voice", "voices", "choir", "chorus", "soprano", "tenor"}
_STOP = {"no", "nr", "n", "num", "in", "op", "opus", "the", "der", "die", "das", "den", "dem", "des", "de", "la",
         "le", "les", "il", "lo", "a", "an", "of", "for", "fur", "pour", "per", "und", "and", "et", "e", "y",
         "major", "minor", "dur", "moll", "flat", "sharp", "opera", "oper", "with", "mit", "von", "zu",
         "from", "aus", "live", "complete", "version", "original", "edition", "nos", "mvt", "mvmt"}
# 'orch.' needs its own ending: '\b' after the period would demand a letter next ('(orch. Ravel)')
_ARRANGED = re.compile(r"\b(?:arr|arranged|arrangement|transcr|transcribed|transcription|bearb|bearbeitung|"
                       r"orch|orchestrated|orchestration|version for|after)(?![a-z])", re.IGNORECASE)
_KEY_TEXT = re.compile(r"\bin\s+[a-g](?:[\s-]*(?:flat|sharp|♭|♯))?(?:\s+(?:major|minor))?\b|"
                       r"\b[a-h](?:is|es|s)?[\s-](?:dur|moll)\b", re.IGNORECASE)
_ROMAN_NO = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


@dataclass(frozen=True, slots=True)
class TitleSig:
    key: str | None
    numbers: frozenset[int]
    forms: frozenset[str]
    scoring: frozenset[str]
    names: frozenset[str]


def title_sig(title: str) -> TitleSig:
    """What a title says besides its catalogue number: key, numbers, forms, scoring and name words."""
    t, _ = split_act_suffix(strip_track_number(title))
    key = cat.parse_key(t)
    s = cat.strip_catalogues(fold(t))
    s = _KEY_TEXT.sub(" ", s)
    nums: set[int] = set()
    for m in re.finditer(r"(?:\b(?:no|nr|n°|num)\.?\s*|#\s*)(\d{1,3}|[ivx]{1,4})\b", s):
        v = m.group(1)
        nums.add(int(v) if v.isdigit() else _ROMAN_NO.get(v, 0))
    s = re.sub(r"(?:\b(?:no|nr|n°|num)\.?\s*|#\s*)(\d{1,3}|[ivx]{1,4})\b", " ", s)
    nums |= {int(n) for n in re.findall(r"\b(\d{1,3})\b", s)}          # 4-digit years are not numbers
    forms: set[str] = set()
    scoring: set[str] = set()
    names: set[str] = set()
    # name words in brackets are mostly translations ('(The Flying Dutchman)'): they never decide
    outer = set(re.findall(r"[a-z]+", re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", s)))
    for w in re.findall(r"[a-z]+", s):
        if w in _FORMS:
            forms.add(_FORMS[w])
        elif w in _SCORING:
            scoring.add(w)
        elif w not in _STOP and len(w) >= 3 and w in outer:
            names.add(w)
    return TitleSig(key, frozenset(nums), frozenset(forms), frozenset(scoring), frozenset(names))


def _word_match(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5])     # Leonore ~ Leonoren


def titles_agree(a: str, b: str) -> str | None:
    """None when two titles with the same catalogue number can name the same work, else why not."""
    x, y = title_sig(a), title_sig(b)
    if x.key and y.key and x.key != y.key:
        return "different key"
    if x.numbers != y.numbers:
        return "different numbers"
    if x.forms != y.forms:
        return "different form"
    if x.scoring and y.scoring and x.scoring != y.scoring:
        return "different instruments"
    # every name word must have its match on the other side: 'Prelude' is not 'Prelude and Liebestod', the
    # opera is not 'Die Zauberflöte: Der Hölle Rache' (one side naming nothing, 'Symphony No. 9', still fits)
    if x.names and y.names and not (all(any(_word_match(p, q) for q in y.names) for p in x.names)
                                    and all(any(_word_match(q, p) for p in x.names) for q in y.names)):
        return "different names"
    return None


def is_arrangement(title: str | None) -> bool:
    return bool(_ARRANGED.search(fold(title or "")))


def cross_keys(composer: str | None, title: str, catalogues: list[cat.Catalogue]) -> list[tuple[str, str]]:
    """The cross-album keys of one work, strongest first: ('known', 'known:fidelio'), ('cat', 'cat:op:72a').
    Arrangements get none (an arrangement is its own work, §5.5)."""
    if is_arrangement(title):
        return []
    out: list[tuple[str, str]] = []
    base, _ = split_act_suffix(title)
    kw = known_work(composer, base)
    if kw:
        out.append(("known", f"known:{kw[0]}"))
    if catalogues:
        out.append(("cat", f"cat:{catalogues[0].key}"))
    return out


def has_act_suffix(title: str) -> bool:
    return split_act_suffix(title)[1] is not None
