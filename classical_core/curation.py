"""Today's shelves: picks tied to the date, the season and the day of the week (PLAN.md §5.9, scoped;
DEVIATIONS.md). MA-free and pure: everything takes the day (and hemisphere) as arguments.

Ported in spirit from Sethster's calendar and themed programming (on-this-day composer anniversaries and
premieres, composer months, Mozart Monday / Wagner Wednesday / Bach Sunday, Optoberfest building to
Halloween), re-done for a personal library:

- **On this day**: composers born (or, where the birth day is unrecorded, baptised) or died on this day
  (`composers._DAYS`), and premieres on this day (`data/premieres.json`, sources cited per entry). When fewer
  than three works qualify, the window widens to three days either side and each reason gives the real date.
  Round anniversaries (multiples of 25 years) weigh more.
- **For the season**: church seasons and feasts (Advent, Christmas, Holy Week, Easter, Pentecost, and Bach's
  cantatas for the fixed feasts), Halloween and the October run-up to it, All Souls, Remembrance Day, New
  Year, Valentine's Day, the composer of the month, and the natural season (meteorological, flipped for the
  southern hemisphere).
- **For <weekday>**: the featured composer (Mozart Monday, Wagner Wednesday, the Bach cantata for this
  Sunday in the Lutheran calendar Bach wrote for) and then the day's mood by work type.
- **Acclaimed recordings**: recordings whose accolades count (§5.8).

Every pick is a whole recording of a work (its Play pick, `accolades.best_pick`), weighted by
`accolades.pick_score` (acclaim x evidence quality). A shelf never holds two works by one composer, and a
work appears on at most one shelf a day. Choices are a weighted shuffle seeded by the date, so they are
stable all day and differ from day to day and year to year.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from . import accolades, composers, dates
from .browse import play_uri
from .text import norm_key, surname

SIZE = 6
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HEMISPHERES = ("north", "south")


def fmt_day(d: dt.date, year: bool = False) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---- the church year -----------------------------------------------------------------------------------


def easter(year: int) -> dt.date:
    """Western (Gregorian) Easter Sunday: the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def advent_sunday(year: int) -> dt.date:
    """First Sunday of Advent: the Sunday from 27 November to 3 December."""
    d = dt.date(year, 11, 27)
    return d + dt.timedelta(days=(6 - d.weekday()) % 7)


# Bach's surviving church cantatas for each Sunday and feast of the Lutheran year (BWV numbers), after the
# Bach Digital and Wikipedia ("List of Bach cantatas by liturgical occasion") tables.
_AFTER_TRINITY: dict[int, tuple[str, ...]] = {
    1: ("75", "20", "39"), 2: ("76", "2"), 3: ("21", "135"), 4: ("24", "177", "185"), 5: ("93", "88"),
    6: ("170", "9"), 7: ("186", "107", "187"), 8: ("136", "178", "45"), 9: ("105", "94", "168"),
    10: ("46", "101", "102"), 11: ("199", "179", "113"), 12: ("69a", "137", "35"), 13: ("77", "33", "164"),
    14: ("25", "78", "17"), 15: ("138", "99", "51"), 16: ("161", "95", "8", "27"), 17: ("148", "114", "47"),
    18: ("96", "169"), 19: ("48", "5", "56"), 20: ("162", "180", "49"), 21: ("109", "38", "98", "188"),
    22: ("89", "115", "55"), 23: ("163", "139", "52"), 24: ("60", "26"), 25: ("90", "116"), 26: ("70",),
    27: ("140",),
}
_AFTER_EPIPHANY: dict[int, tuple[str, ...]] = {1: ("154", "124", "32"), 2: ("155", "3", "13"),
                                               3: ("73", "111", "72", "156"), 4: ("81", "14")}
# Sundays counted from Easter: (offset in days, name, cantatas)
_FROM_EASTER: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (-63, "Septuagesima", ("144", "92", "84")), (-56, "Sexagesima", ("18", "181", "126")),
    (-49, "Estomihi", ("22", "23", "127", "159")), (-42, "Invocavit (1st Sunday in Lent)", ()),
    (-35, "Reminiscere (2nd Sunday in Lent)", ()), (-28, "Oculi (3rd Sunday in Lent)", ("54",)),
    (-21, "Laetare (4th Sunday in Lent)", ()), (-14, "Judica (5th Sunday in Lent)", ()),
    (-7, "Palm Sunday", ("182",)), (0, "Easter Sunday", ("4", "31", "249")),
    (7, "Quasimodogeniti", ("67", "42")), (14, "Misericordias Domini", ("104", "85", "112")),
    (21, "Jubilate", ("12", "103", "146")), (28, "Cantate", ("166", "108")), (35, "Rogate", ("86", "87")),
    (42, "Exaudi", ("44", "183")), (49, "Pentecost", ("172", "59", "74", "34")),
    (56, "Trinity Sunday", ("165", "176", "129", "194")),
)
# feasts on fixed dates (MM-DD) and movable ones (days from Easter)
_FIXED_FEASTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "12-25": ("Christmas Day", ("63", "91", "110", "248")), "12-26": ("Second Day of Christmas", ("40", "121", "57")),
    "12-27": ("Third Day of Christmas", ("64", "133", "151")), "01-01": ("New Year's Day", ("190", "41", "16", "171", "143")),
    "01-06": ("Epiphany", ("65", "123", "248")), "02-02": ("Purification of Mary", ("83", "125", "82", "157", "158")),
    "03-25": ("Annunciation", ("1",)), "06-24": ("St John the Baptist", ("167", "7", "30")),
    "07-02": ("Visitation", ("147", "10")), "09-29": ("St Michael and All Angels", ("130", "19", "149", "50")),
    "10-31": ("Reformation Day", ("80", "79", "192")),
}
_MOVABLE_FEASTS: dict[int, tuple[str, tuple[str, ...]]] = {
    -2: ("Good Friday", ("245", "244")), 1: ("Easter Monday", ("66", "6")), 2: ("Easter Tuesday", ("134", "158")),
    39: ("Ascension", ("37", "128", "43", "11")), 50: ("Whit Monday", ("173", "68", "174")),
    51: ("Whit Tuesday", ("184", "175")),
}


@dataclass(frozen=True, slots=True)
class Liturgical:
    name: str
    cantatas: tuple[str, ...]        # BWV numbers ("69a" included)


def liturgical_sunday(day: dt.date) -> Liturgical | None:
    """The Sunday's name in the Lutheran calendar of Bach's Leipzig and its cantatas; None on a weekday."""
    if day.weekday() != 6:
        return None
    y = day.year
    adv = advent_sunday(y)
    if day >= adv:
        n = (day - adv).days // 7 + 1
        if n <= 4 and day < dt.date(y, 12, 25):
            return Liturgical(f"{ordinal(n)} Sunday of Advent",
                              {1: ("61", "62", "36"), 2: ("70a",), 3: ("186a",), 4: ("132", "147a")}[n])
    md = day.strftime("%m-%d")
    if md in ("12-25", "12-26", "01-01", "01-06"):
        f = _FIXED_FEASTS[md]
        return Liturgical(f[0], f[1])
    if day.month == 12 and day.day >= 27:
        return Liturgical("Sunday after Christmas", ("152", "122", "28"))
    if day.month == 1 and day.day < 6:
        return Liturgical("Sunday after New Year", ("153", "58"))
    e = easter(y)
    off = (day - e).days
    for o, name, bwvs in _FROM_EASTER:
        if o == off:
            return Liturgical(name, bwvs)
    if off > 56:
        n = (off - 56) // 7
        return Liturgical(f"{ordinal(n)} Sunday after Trinity", _AFTER_TRINITY.get(n, ()))
    # between Epiphany and Septuagesima
    first = dt.date(y, 1, 7) + dt.timedelta(days=(6 - dt.date(y, 1, 7).weekday()) % 7)
    n = (day - first).days // 7 + 1
    return Liturgical(f"{ordinal(n)} Sunday after Epiphany", _AFTER_EPIPHANY.get(n, ()))


def feast(day: dt.date) -> Liturgical | None:
    """A feast day with Bach cantatas (fixed or counted from Easter), other than a Sunday's own name."""
    f = _FIXED_FEASTS.get(day.strftime("%m-%d")) or _MOVABLE_FEASTS.get((day - easter(day.year)).days)
    return Liturgical(*f) if f else None


# ---- candidates ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cand:
    work_id: str
    title: str
    composer: str | None
    kc: composers.KnownComposer | None
    ckey: str                        # one per composer: the canonical name, else the folded raw name
    ident: str                       # the same work indexed twice (two releases' titles) shares this
    work_type: str | None
    text: str                        # norm_key of title and catalogue number
    rec: dict
    score: float                     # accolades.pick_score of the Play pick
    acclaim: float
    accolade: str | None             # the strongest counted accolade's text
    premiered: str | None = None     # the stored premiere (dates.py): a full date makes an anniversary
    premiered_place: str | None = None
    premiered_source: str | None = None


def _cand(w: dict, rec: dict, accs: list[dict]) -> Cand:
    kc = composers.lookup(w.get("composer")) or composers.find_in_credit(w.get("composer"))
    counted = [a for a in accs if a.get("counts")]
    top = max(counted, key=lambda a: a.get("weight", 1.0), default=None)
    ckey = kc.name if kc else norm_key(w.get("composer") or "")
    k = accolades.work_key(w["title"])
    ident = f"{ckey}|{' '.join(sorted(k.tokens))}|{sorted(k.numbers)}|{k.key}"
    return Cand(w["id"], w["title"], w.get("composer"), kc, ckey, ident, w.get("work_type"), norm_key(f"{w['title']} {w.get('catalogue_display') or ''}"), rec,
                accolades.pick_score(rec, accs), accolades.acclaim(accs), top["text"] if top else None,
                w.get("premiered") if w.get("premiered_precision") == "day" else None, w.get("premiered_place"),
                w.get("premiered_source"))


def candidates(ix, vis=None) -> list[Cand]:
    """Every visible work with its Play pick (index.Index; one accolade query for the lot)."""
    per = [(w, ix.recordings(w["id"], vis)) for w in ix.works(vis=vis)]
    accs = accolades.for_recordings(ix.con, [r for _, recs in per for r in recs])
    out = []
    for w, recs in per:
        best = accolades.best_pick(recs, accs)
        if best and not (w.get("work_type") == "Other" and _NOT_MUSIC.search(norm_key(w["title"]))):
            out.append(_cand(w, best[0], accs.get(best[0]["id"], [])))
    return out


_NOT_MUSIC = re.compile(r"\b(?:remarks|interview|applause|talk|announcements?|speech|commentary|discussion)\b")


def _is(c: Cand, who: str) -> bool:
    """Is the work by this composer? Canonical names when both are known, else the surname."""
    return _same_composer(who, c.kc.name if c.kc else None, c.composer)


@lru_cache(maxsize=8192)
def _same_composer(who: str, known: str | None, composer: str | None) -> bool:
    want = composers.lookup(who)
    if want and known:
        return want.name == known
    return bool(composer) and norm_key(surname(who, particles=False)) == \
        norm_key(surname(composer or "", particles=False))


# ---- themes --------------------------------------------------------------------------------------------

# (composer or None for anyone, pattern over Cand.text, strength 0..1, optional note for the reason)
Rule = tuple[str | None, str, float, str | None]


def _bwv(nums: Iterable[str]) -> str:
    return r"\bbwv (?:" + "|".join(re.escape(n) for n in nums) + r")\b"


_MESSIAH = ("George Frideric Handel", r"\bmessiah\b|\bhwv 56\b", 1.0, None)
_SPOOKY: tuple[tuple[str, str, str], ...] = (       # composer, pattern, yes | partial
    ("Camille Saint-Saëns", r"danse macabre|\bop 40\b", "yes"),
    ("Modest Mussorgsky", r"bald mountain|lysaya gora|kahlen berge|mont chauve", "yes"),
    ("Hector Berlioz", r"symphonie fantastique|\bop 14\b|damnation de faust|\bop 24\b", "yes"),
    ("Paul Dukas", r"apprenti sorcier|sorcerer s apprentice", "yes"),
    ("Franz Liszt", r"totentanz|danse macabre|mephisto|faust symphon|dante", "yes"),
    ("Carl Maria von Weber", r"freischutz", "yes"),
    ("Richard Wagner", r"fliegende holl|flying dutchman|\bwwv 63\b", "yes"),
    ("Béla Bartók", r"bluebeard|kekszakallu|\bsz 48\b", "yes"),
    ("Wolfgang Amadeus Mozart", r"don giovanni|\bk 527\b", "yes"),
    ("Sergei Rachmaninoff", r"isle of the dead|toteninsel|\bop 29\b", "yes"),
    ("Antonín Dvořák", (r"noon witch|polednice|water goblin|vodnik|golden spinning|kolovrat|wood dove|holoubek"
                        r"|\bop 10[789]\b|\bop 110\b|\bb 19[6-9]\b"), "yes"),
    ("Franz Schubert", r"erlkonig|erl king|\bd 328\b", "yes"),
    ("Giuseppe Tartini", r"devil s trill|trillo del diavolo", "yes"),
    ("Maurice Ravel", r"gaspard de la nuit", "yes"),
    ("Benjamin Britten", r"turn of the screw", "yes"),
    ("Arrigo Boito", r"mefistofele", "yes"),
    ("Charles Gounod", r"^faust\b", "yes"),
    ("Johann Sebastian Bach", r"\bbwv 565\b", "yes"),
    ("Jean Sibelius", r"tuonela|lemminkainen|\bop 22\b", "partial"),
    ("Modest Mussorgsky", r"pictures at an exhibition|tableaux d une exposition", "partial"),
    ("Edvard Grieg", r"peer gynt|\bop 23\b|\bop 46\b|\bop 55\b", "partial"),
    ("Engelbert Humperdinck", r"hansel", "partial"),
    ("Igor Stravinsky", r"firebird|oiseau de feu|zhar ptitsa", "partial"),
    ("Pyotr Ilyich Tchaikovsky", r"swan lake|\bop 20\b|queen of spades|pique dame|\bop 68\b", "partial"),
    ("Antonín Dvořák", r"rusalka|\bop 114\b", "partial"),
    ("Béla Bartók", r"miraculous mandarin|\bsz 73\b", "partial"),
    ("Niccolò Paganini", r".", "partial"),
    ("Wolfgang Amadeus Mozart", r"requiem|\bk 626\b", "partial"),
    ("Giuseppe Verdi", r"requiem|macbeth", "partial"),
    ("Franz Schubert", r"death and the maiden|tod und das madchen|\bd 810\b|\bd 531\b", "partial"),
    ("Frédéric Chopin", r"sonata no 2\b|\bop 35\b", "partial"),
    ("Ludwig van Beethoven", r"\bghost\b|geister|\bop 70 no 1\b", "partial"),
    ("Richard Strauss", r"\bsalome\b|\bop 54\b|elektra", "partial"),
    ("Richard Wagner", r"walkure", "partial"),
    ("Sergei Prokofiev", r"scythian|ala and lolli", "partial"),
)
_CHRISTMAS: tuple[Rule, ...] = (
    _MESSIAH,
    ("Pyotr Ilyich Tchaikovsky", r"nutcracker|casse noisette|shchelkunchik|\bop 71a?\b", 1.0, None),
    ("Johann Sebastian Bach", r"weihnachtsoratorium|christmas oratorio|\bbwv 248\b", 1.0, None),
    ("Johann Sebastian Bach", _bwv(("63", "91", "110", "40", "121", "57", "64", "133", "151", "243")) + "|magnificat",
     0.8, None),
    ("Arcangelo Corelli", r"op 6 no 8\b|christmas concerto|fatto per la notte", 0.8, None),
    ("Hector Berlioz", r"enfance du christ", 0.8, None),
    ("Benjamin Britten", r"ceremony of carols|\bop 28\b", 0.8, None),
    ("Marc-Antoine Charpentier", r"messe de minuit", 0.8, None),
    ("Camille Saint-Saëns", r"oratorio de noel|\bop 12\b", 0.8, None),
    ("Heinrich Schütz", r"weihnachtshistorie|christmas story|\bswv 435\b", 0.8, None),
    ("Ralph Vaughan Williams", r"christmas carols|\bhodie\b", 0.8, None),
    ("Engelbert Humperdinck", r"hansel", 0.6, None),
    (None, r"\bchristmas\b|\bnoel\b|weihnacht|\bnatale\b|nativity", 0.7, None),
)
_SEASONS: dict[str, tuple[Rule, ...]] = {
    "spring": (
        ("Antonio Vivaldi", r"\bspring\b|primavera|\brv 269\b", 1.0, None),
        ("Igor Stravinsky", r"rite of spring|sacre du printemps", 1.0, None),
        ("Aaron Copland", r"appalachian spring", 1.0, None),
        ("Ludwig van Beethoven", r"\bspring\b|fruhling|\bop 24\b", 0.8, None),
        ("Robert Schumann", r"symphony no 1\b|\bspring\b|\bop 38\b", 0.8, None),
        ("Benjamin Britten", r"spring symphony|\bop 44\b", 0.8, None),
        ("Frederick Delius", r"first cuckoo", 0.8, None),
        (None, r"\bspring\b|fruhling|printemps|primavera", 0.6, None),
    ),
    "summer": (
        ("Antonio Vivaldi", r"\bsummer\b|\bestate\b|\brv 315\b", 1.0, None),
        ("Felix Mendelssohn", r"midsummer|sommernachtstraum|\bop 21\b|\bop 61\b", 1.0, None),
        ("Hector Berlioz", r"nuits d ete|\bop 7\b", 0.8, None),
        ("Samuel Barber", r"knoxville", 0.8, None),
        ("Gustav Mahler", r"symphony no 3\b", 0.6, "Pan awakes: summer marches in"),
        ("Ludwig van Beethoven", r"symphony no 6\b|pastoral|\bop 68\b", 0.6, None),
        ("Johannes Brahms", r"symphony no 2\b|\bop 73\b", 0.4, "written by a summer lake at Pörtschach"),
        (None, r"\bsummer\b|sommer|\bete\b|midsummer", 0.6, None),
    ),
    "autumn": (
        ("Antonio Vivaldi", r"\bautumn\b|autunno|\brv 293\b", 1.0, None),
        ("Richard Strauss", r"four last songs|vier letzte lieder", 1.0, None),
        ("Pyotr Ilyich Tchaikovsky", r"\bseasons\b|\bop 37a?\b", 0.6, None),
        ("Alexander Glazunov", r"\bseasons\b|\bop 67\b", 0.6, None),
        ("Joseph Haydn", r"jahreszeiten|the seasons|xxi 3\b", 0.6, None),
        ("Gustav Mahler", r"lied von der erde", 0.6, "Der Einsame im Herbst"),
        ("Edward Elgar", r"cello concerto|\bop 85\b", 0.5, "Elgar's autumnal elegy"),
        ("Johannes Brahms", r"symphony no 4\b|\bop 98\b|clarinet quintet|\bop 115\b", 0.5, "autumnal Brahms"),
        (None, r"\bautumn\b|\bherbst|automne|autunno", 0.6, None),
    ),
    "winter": (
        ("Antonio Vivaldi", r"\bwinter\b|inverno|\brv 297\b", 1.0, None),
        ("Franz Schubert", r"winterreise|\bd 911\b", 1.0, None),
        ("Pyotr Ilyich Tchaikovsky", r"winter daydreams|symphony no 1\b|\bop 13\b", 0.8, None),
        ("Ralph Vaughan Williams", r"antartica|symphony no 7\b", 0.6, None),
        ("Sergei Prokofiev", r"winter bonfire|\bop 122\b", 0.6, None),
        ("Jean Sibelius", r".", 0.3, "Nordic winter"),
        (None, r"\bwinter\b|hiver|inverno|\bsnow", 0.6, None),
    ),
}
# Sethster's composer months (themed-programming requirements)
_COMPOSER_MONTH = {1: "Wolfgang Amadeus Mozart", 2: "George Frideric Handel", 3: "Johann Sebastian Bach",
                   5: "Pyotr Ilyich Tchaikovsky", 10: "Giuseppe Verdi", 12: "Ludwig van Beethoven"}


@dataclass(frozen=True, slots=True)
class Theme:
    id: str
    title: str
    tier: int                        # 0 holidays and feasts, 1 the composer of the month and the natural season
    weight: float
    rules: tuple[Rule, ...]


def natural_season(day: dt.date, hemisphere: str = "north") -> str:
    """Meteorological season (Dec–Feb winter in the north); the southern hemisphere is six months on."""
    m = day.month if hemisphere != "south" else (day.month + 5) % 12 + 1
    return ("winter", "spring", "summer", "autumn")[(m % 12) // 3]


def themes(day: dt.date, hemisphere: str = "north") -> list[Theme]:
    """The season shelf's themes active on this day, most specific first."""
    y, md = day.year, day.strftime("%m-%d")
    e = easter(y)
    off = (day - e).days
    out: list[Theme] = []

    def add(id_: str, title: str, rules: Iterable[Rule], weight: float = 3.0, tier: int = 0) -> None:
        out.append(Theme(id_, title, tier, weight, tuple(rules)))

    f = feast(day)
    if f and f.cantatas:
        add("feast", f.name, [("Johann Sebastian Bach", _bwv(f.cantatas), 1.0, f"Bach for {f.name}")], 4.0)
    if md in ("12-31", "01-01"):
        add("new_year", "New Year", [
            ("Johann Strauss II", r".", 1.0, None), ("Josef Strauss", r".", 0.8, None),
            ("Ludwig van Beethoven", r"symphony no 9\b|\bop 125\b", 1.0, "Beethoven's Ninth for the New Year"),
            ("Franz Lehár", r"lustige witwe|merry widow", 0.8, None)], 4.0)
    if md >= "12-24" or md <= "01-06":
        add("christmas", "Christmas", _CHRISTMAS, 4.0 if md in ("12-24", "12-25") else 3.0)
    elif advent_sunday(y) <= day < dt.date(y, 12, 24):
        add("advent", "Advent", [
            ("Johann Sebastian Bach", _bwv(("61", "62", "36", "132", "70a", "147", "659")), 1.0, None),
            ("George Frideric Handel", r"\bmessiah\b|\bhwv 56\b", 0.8, None),
            ("Pyotr Ilyich Tchaikovsky", r"nutcracker|casse noisette|\bop 71a?\b", 0.6, None),
            (None, r"\badvent\b|\bnoel\b|\bchristmas\b|weihnacht", 0.6, None)])
    if -46 <= off < -7:
        add("lent", "Lent", [(None, r"miserere|stabat mater|lamentation|de profundis|tenebrae", 1.0, None),
                             ("Joseph Haydn", r"seven last words|sieben letzten|xx 1|xx 2", 0.8, None)], 2.0)
    elif -7 <= off < 0:
        add("holy_week", "Holy Week", [
            ("Johann Sebastian Bach", r"\bbwv 24[457]\b|passion", 1.0, None),
            ("Joseph Haydn", r"seven last words|sieben letzten|xx 1|xx 2", 0.9, None),
            ("Richard Wagner", r"parsifal|\bwwv 111\b", 0.8, "the Good Friday music"),
            ("Ludwig van Beethoven", r"olbergen|mount of olives|\bop 85\b", 0.8, None),
            ("Gregorio Allegri", r"miserere", 0.9, None),
            (None, r"\bpassion\b|passio\b|stabat mater|lamentation|tenebres|tenebrae|seven last words", 0.8, None)],
            4.0 if off == -2 else 3.0)
    elif 0 <= off <= 7:
        add("easter", "Easter", [
            ("Johann Sebastian Bach", r"osteroratorium|easter oratorio|" + _bwv(("249", "4", "31", "66", "6")),
             1.0, None),
            ("Gustav Mahler", r"symphony no 2\b|resurrection|auferstehung", 1.0, None),
            ("Nikolai Rimsky-Korsakov", r"russian easter|\bop 36\b", 1.0, None),
            ("George Frideric Handel", r"resurrezione|\bhwv 47\b", 0.9, None), _MESSIAH[:2] + (0.6, None),
            (None, r"\beaster\b|resurrect|auferstehung|\bostern", 0.8, None)], 4.0 if off == 0 else 3.0)
    elif 49 <= off <= 51:
        add("pentecost", "Pentecost", [
            ("Gustav Mahler", r"symphony no 8\b", 1.0, "Veni, creator spiritus"),
            (None, r"veni creator|pentecost|pfingst|whitsun", 0.8, None)])
    if dt.date(y, 10, 1) <= day <= dt.date(y, 10, 30):
        ramp = (day.day - 1) / 29                     # Optoberfest: a crescendo towards Halloween
        add("optoberfest", "Optoberfest", [(c, p, 0.6 + 0.4 * ramp if s == "yes" else 0.6 - 0.4 * ramp, None)
                                           for c, p, s in _SPOOKY], 2.0 + ramp)
    elif md == "10-31":
        add("halloween", "Halloween", [(c, p, 1.0, None) for c, p, s in _SPOOKY if s == "yes"], 4.0)
    if md in ("11-01", "11-02"):
        add("all_souls", "All Saints and All Souls", [
            (None, r"requiem|totenmesse|messe des morts|pro defunctis|funeral", 1.0, None),
            ("Johann Sebastian Bach", r"\bbwv 106\b|actus tragicus", 1.0, None)])
    if md == "11-11":
        add("remembrance", "Remembrance Day", [
            ("Benjamin Britten", r"war requiem|\bop 66\b", 1.0, None),
            ("Edward Elgar", r"spirit of england|for the fallen|\bop 80\b", 1.0, None),
            ("Ralph Vaughan Williams", r"pastoral symphony|symphony no 3\b", 0.9, None),
            ("Maurice Ravel", r"tombeau de couperin", 0.9, None),
            ("Samuel Barber", r"adagio for strings", 0.9, None),
            ("Dmitri Shostakovich", r"symphony no [78]\b|leningrad|\bop 6[05]\b", 0.8, None),
            (None, r"war requiem|for the fallen|in memoriam|threnody", 0.7, None)])
    if md == "02-14":
        add("valentine", "Valentine's Day", [
            (None, r"romeo|tristan|liebestr|dichterliebe|frauenliebe|siegfried idyll|salut d amour", 1.0, None),
            ("Gustav Mahler", r"symphony no 5\b", 0.7, "the Adagietto, a love letter to Alma"),
            (None, r"\blove\b|\bliebe|\bamour\b|amore", 0.6, None)])
    north = hemisphere != "south"
    if (north and day.month == 6 or not north and day.month == 12) and 19 <= day.day <= 25:
        add("midsummer", "Midsummer", [
            ("Felix Mendelssohn", r"midsummer|sommernachtstraum|\bop 21\b|\bop 61\b", 1.0, None),
            ("Benjamin Britten", r"midsummer", 1.0, None),
            (None, r"midsummer|midsommar|st john s eve", 0.8, None)], 2.0)
    if day.month in _COMPOSER_MONTH:
        who = _COMPOSER_MONTH[day.month]
        add("composer_month", f"{surname(who, particles=False)} month", [(who, r".", 1.0, None)], 1.2, tier=1)
    season = natural_season(day, hemisphere)
    add(season, season.capitalize(), _SEASONS[season], 1.0, tier=1)
    return out


# ---- on this day ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Premiere:
    composer: str
    work: str
    match: re.Pattern[str]
    date: dt.date
    place: str
    source: str
    note: str | None


@lru_cache(maxsize=1)
def premieres() -> tuple[Premiere, ...]:
    doc = json.loads(resources.files("classical_core.data").joinpath("premieres.json").read_text("utf-8"))
    return tuple(Premiere(p["composer"], p["work"], re.compile(p["match"]), dt.date.fromisoformat(p["date"]),
                          p["place"], p["source"], p.get("note")) for p in doc["items"])


def _on(year: int, md: str) -> dt.date:
    """This year's date for an anniversary; 29 February falls on the 28th outside leap years."""
    m, d = int(md[:2]), int(md[3:])
    if (m, d) == (2, 29) and not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        d = 28
    return dt.date(year, m, d)


def _gap(day: dt.date, md: str) -> int:
    """Days from the anniversary to `day`, nearest across a year end (-3..3 within the widened window)."""
    best = min(((day - _on(y, md)).days for y in (day.year - 1, day.year, day.year + 1)), key=abs)
    return best


def _years(day: dt.date, md: str, year: int) -> int:
    ann = _on(day.year, md)
    if (day - ann).days > 180:
        ann = _on(day.year + 1, md)
    elif (ann - day).days > 180:
        ann = _on(day.year - 1, md)
    return ann.year - year


@dataclass(frozen=True, slots=True)
class Event:
    kind: str                        # premiere | born | baptised | died
    gap: int                         # days since the anniversary (0 = today, negative = coming up)
    years: int
    when: dt.date | None             # the original date (None when only the month/day and year are known)
    md: str
    year: int
    who: str                         # composer
    premiere: Premiere | None = None

    @property
    def round(self) -> bool:
        return self.years > 0 and self.years % 25 == 0

    def reason(self) -> str:
        today = self.gap == 0
        ago = f"{ordinal(self.years)} anniversary" if self.round else f"{self.years} years ago"
        if self.kind == "premiere":
            p = self.premiere
            assert p is not None
            head = f"Premiered on this day in {p.date.year}" if today else f"Premiered {fmt_day(p.date, True)}"
            return f"{head}, {p.place} ({ago})" if p.place else f"{head} ({ago})"
        verb = {"born": "Born", "baptised": "Baptised", "died": "Died"}[self.kind]
        name = surname(self.who, particles=False)
        when = f"on this day in {self.year}" if today else \
            f"{int(self.md[3:])} {MONTHS[int(self.md[:2]) - 1]} {self.year}"
        tail = " (birth date unrecorded)" if self.kind == "baptised" else ""
        return f"{name}: {verb} {when}{tail} ({ago})"


def events(day: dt.date, cands: list[Cand], window: int = 0) -> list[tuple[Event, Cand]]:
    """Anniversaries within `window` days of `day` that touch a work in the library."""
    out: list[tuple[Event, Cand]] = []
    # a work whose premiere the dates job settled (a day from any source, or the user's 'unknown') uses
    # that; the others keep the bundled list's own matching
    settled = [c for c in cands if c.premiered or c.premiered_source]
    for c in settled:
        if not c.premiered:
            continue
        when = dt.date.fromisoformat(c.premiered)
        md = when.strftime("%m-%d")
        g = _gap(day, md)
        if abs(g) <= window:
            p = Premiere(c.kc.name if c.kc else c.composer or "", c.title, re.compile(r"(?!)"), when,
                         c.premiered_place or "", "", None)
            out.append((Event("premiere", g, _years(day, md, when.year), when, md, when.year, p.composer, p), c))
    rest = [c for c in cands if not (c.premiered or c.premiered_source)]
    for p in premieres():
        md = p.date.strftime("%m-%d")
        g = _gap(day, md)
        if abs(g) > window:
            continue
        ev = Event("premiere", g, _years(day, md, p.date.year), p.date, md, p.date.year, p.composer, p)
        out += [(ev, c) for c in rest if _is(c, p.composer) and dates.dataset_hit(c.title, c.text, c.work_type, p)]
    for kc in {c.kc for c in cands if c.kc}:
        for kind, md, year in (("baptised" if kc.baptised else "born", kc.born_md, kc.born),
                               ("died", kc.died_md, kc.died)):
            if not md or not year:
                continue
            g = _gap(day, md)
            if abs(g) > window:
                continue
            ev = Event(kind, g, _years(day, md, year), None, md, year, kc.name)
            out += [(ev, c) for c in cands if c.kc == kc]
    return out


# ---- choosing ------------------------------------------------------------------------------------------


def _key(day: dt.date, shelf: str, wid: str, weight: float) -> float:
    """Efraimidis–Spirakis: log(u)/w, larger first; u is a hash of the day, the shelf and the work."""
    h = int.from_bytes(hashlib.sha256(f"{day.isoformat()}|{shelf}|{wid}".encode()).digest()[:8], "big")
    u = (h + 1) / (2**64 + 2)
    return math.log(u) / max(weight, 1e-6)


Entry = tuple[int, float, Cand, str]  # tier (lower first), weight, candidate, reason


def choose(entries: Iterable[Entry], day: dt.date, shelf: str, used: set[str], size: int = SIZE) -> list[dict]:
    """Up to `size` picks: lower tiers first, then a weighted shuffle seeded by the day; one work per composer
    and no work already on another shelf today (`used` holds `Cand.ident`s)."""
    best: dict[str, Entry] = {}
    for e in entries:                 # a work matched twice keeps its strongest reason
        cur = best.get(e[2].work_id)
        if cur is None or (e[0], -e[1]) < (cur[0], -cur[1]):
            best[e[2].work_id] = e
    ranked = sorted(best.values(), key=lambda e: (e[0], -_key(day, shelf, e[2].work_id, e[1])))
    picks: list[dict] = []
    seen: set[str] = set()
    for _, _, c, reason in ranked:
        if c.ident in used or c.ckey in seen:
            continue
        seen.add(c.ckey)
        used.add(c.ident)
        picks.append(pick(c, reason))
        if len(picks) >= size:
            break
    return picks


def pick(c: Cand, reason: str) -> dict:
    r = c.rec
    return {"work_id": c.work_id, "title": c.title, "composer": c.kc.name if c.kc else c.composer,
            "work_type": c.work_type, "recording_id": r["id"], "recording_label": r.get("label"),
            "image": r.get("image"), "reason": reason, "accolade": c.accolade, "score": c.score,
            "play_uri": play_uri(c.work_id, r["id"])}


def _match(rules: Iterable[Rule], c: Cand) -> tuple[float, str | None] | None:
    hit = None
    for who, pat, strength, note in rules:
        if pat == "." and c.work_type == "Other":
            continue                  # a composer-wide rule wants real works, not stray tracks
        if (who is None or _is(c, who)) and re.search(pat, c.text) and (hit is None or strength > hit[0]):
            hit = (strength, note)
    return hit


# ---- the shelves ---------------------------------------------------------------------------------------


def on_this_day(cands: list[Cand], day: dt.date, used: set[str], size: int = SIZE) -> dict:
    hits = events(day, cands, 0)
    window = 0
    if len({c.work_id for _, c in hits}) < 3:
        window = 3
        hits = events(day, cands, window)
    entries: list[Entry] = []
    for ev, c in hits:
        tier = (0 if ev.gap == 0 else 2) + (ev.kind != "premiere")
        entries.append((tier, c.score * (3.0 if ev.round else 1.0) / (1 + abs(ev.gap)), c, ev.reason()))
    return {"id": "on_this_day", "title": "On this day",
            "subtitle": fmt_day(day) if window == 0 else f"Around {fmt_day(day)}",
            "picks": choose(entries, day, "on_this_day", used, size)}


def for_season(cands: list[Cand], day: dt.date, hemisphere: str, used: set[str], size: int = SIZE) -> dict:
    ts = themes(day, hemisphere)
    entries: list[Entry] = []
    for c in cands:
        for t in ts:
            m = _match(t.rules, c)
            if m:
                entries.append((t.tier, c.score * t.weight * m[0], c, t.title + (f": {m[1]}" if m[1] else "")))
    picks = choose(entries, day, "season", used, size)
    shown = [t.title for t in ts if any(p["reason"].startswith(t.title) for p in picks)]
    return {"id": "season", "title": "For the season", "subtitle": " · ".join(shown or [ts[0].title]),
            "picks": picks}


_DAY_TYPES = {0: ("Keyboard", "Chamber"), 1: ("Concertos",), 2: ("Orchestral",), 3: ("Chamber", "Vocal"),
              4: ("Opera & Stage",), 5: ("Symphonies",), 6: ("Choral & Sacred",)}
_DAY_MOOD = {0: "keyboard and chamber music for a gentle start", 1: "concertos", 2: "orchestral music",
             3: "chamber music and song", 4: "Friday night at the opera", 5: "big symphonies for a Saturday",
             6: "Sunday morning: sacred music"}
_DAY_REASON = {0: "A gentle Monday start", 1: "Concerto Tuesday", 2: "Orchestral Wednesday",
               3: "Thursday chamber music and song", 4: "Friday night at the opera", 5: "A Saturday symphony",
               6: "Sunday morning: sacred music"}


def _real(c: Cand) -> bool:
    return c.work_type != "Other"


def weekday_rules(day: dt.date) -> tuple[list[tuple[int, str | None, Callable[[Cand], bool], float, str]], list[str]]:
    """The weekday shelf's rules (tier, composer, test, weight, reason) and the day's featured themes."""
    wd, month = day.weekday(), day.month
    rules: list[tuple[int, str | None, Callable[[Cand], bool], float, str]] = []
    feature: list[str] = []
    if wd == 0:
        if month == 12:
            rules.append((0, "George Frideric Handel", lambda c: bool(re.search(_MESSIAH[1], c.text)), 1.0,
                          "Messiah Monday"))
            feature.append("Messiah Monday")
        if month == 7:
            rules.append((1, "Gustav Mahler", _real, 1.0, "Mahler Monday"))
            feature.append("Mahler Monday")
        else:
            rules.append((1, "Wolfgang Amadeus Mozart", _real, 1.0, "Mozart Monday"))
            feature.append("Mozart Monday")
    elif wd == 2:
        rules.append((1, "Richard Wagner", _real, 1.0, "Wagner Wednesday"))
        feature.append("Wagner Wednesday")
    elif wd == 6:
        lit = liturgical_sunday(day)
        if lit and lit.cantatas:
            pat = _bwv(lit.cantatas)
            rules.append((0, "Johann Sebastian Bach", lambda c, pat=pat: bool(re.search(pat, c.text)), 1.0,
                          f"Bach's cantata for {'the ' if lit.name[0].isdigit() else ''}{lit.name}"))
        rules.append((1, "Johann Sebastian Bach", lambda c: c.work_type == "Choral & Sacred", 1.0,
                      "Bach on a Sunday"))
        feature.append(lit.name if lit else "Sunday")
    if wd in (5, 6) and month == 9:
        rules.append((1, "Dmitri Shostakovich", _real, 1.0, f"Shostakovich {WEEKDAYS[wd]}"))
        feature.append(f"Shostakovich {WEEKDAYS[wd]}")
    for t in _DAY_TYPES[wd]:
        rules.append((2, None, lambda c, t=t: c.work_type == t, 1.0, _DAY_REASON[wd]))
    return rules, feature


def for_weekday(cands: list[Cand], day: dt.date, used: set[str], size: int = SIZE) -> dict:
    rules, feature = weekday_rules(day)
    entries: list[Entry] = []
    for c in cands:
        for tier, who, test, w, reason in rules:
            if (who is None or _is(c, who)) and test(c):
                entries.append((tier, c.score * w, c, reason))
    wd = WEEKDAYS[day.weekday()]
    picks = choose(entries, day, "weekday", used, size)
    if len(picks) < 3:               # a small library: top up with anything, still one per composer
        rest = [(3, c.score, c, f"A {wd} pick") for c in cands if _real(c) and c.ckey not in {_ck(p) for p in picks}]
        picks += choose(rest, day, "weekday", used, 3 - len(picks))
    mood = _DAY_MOOD[day.weekday()]
    shown = [f for i, f in enumerate(feature)          # a Sunday's name always shows; a feature, when it played
             if (i == 0 and day.weekday() == 6) or any(f in p["reason"] for p in picks)]
    return {"id": "weekday", "title": f"For {wd}", "subtitle": " · ".join(shown + [mood[0].upper() + mood[1:]]),
            "picks": picks}


def _ck(p: dict) -> str:
    kc = composers.lookup(p["composer"])
    return kc.name if kc else norm_key(p["composer"] or "")


def acclaimed(cands: list[Cand], day: dt.date, used: set[str], size: int = SIZE) -> dict:
    entries = [(0, c.score, c, c.accolade or "Acclaimed") for c in cands if c.acclaim > 1.0]
    return {"id": "acclaimed", "title": "Acclaimed recordings", "subtitle": "Award-winning and highly rated",
            "picks": choose(entries, day, "acclaimed", used, size)}


def today(cands: list[Cand], day: dt.date, hemisphere: str = "north", size: int = SIZE) -> list[dict]:
    """The day's shelves in order; empty ones are left out."""
    used: set[str] = set()
    shelves = [on_this_day(cands, day, used, size), for_season(cands, day, hemisphere, used, size),
               for_weekday(cands, day, used, size), acclaimed(cands, day, used, size)]
    return [s for s in shelves if s["picks"]]


def shelves(ix, day: dt.date, hemisphere: str = "north", vis=None, size: int = SIZE) -> list[dict]:
    return today(candidates(ix, vis), day, hemisphere, size)


def titles(day: dt.date) -> list[tuple[str, str]]:
    """The shelves' ids and names without touching the index (home rows before the picks are ready)."""
    return [("on_this_day", "On this day"), ("season", "For the season"),
            ("weekday", f"For {WEEKDAYS[day.weekday()]}"), ("acclaimed", "Acclaimed recordings")]


def header(day: dt.date, hemisphere: str = "north") -> dict:
    """What the Today view says about the day itself."""
    if hemisphere not in HEMISPHERES:
        raise ValueError("hemisphere must be north or south")
    lit = liturgical_sunday(day) or feast(day)
    return {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "label": fmt_day(day, True),
            "liturgical": lit.name if lit else None, "season": natural_season(day, hemisphere),
            "hemisphere": hemisphere}
