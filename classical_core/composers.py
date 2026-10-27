"""Composer authority list for Tier 3 (PLAN.md §5.4).

A small bundled list of composers with their common spellings, matched **exactly** after `norm_key`
folding. Bare surnames ("Bach", "Strauss") never match on their own; they abstain unless a catalogue prefix
on the same title names one composer (`CATALOGUE_COMPOSER` in the vendored Sethster normalizers), and even
then the prefix only raises confidence. The plan's Wikidata-built list is deferred (DEVIATIONS.md); names
not on this list simply aren't recognised from the artist field, and tags or MusicBrainz still apply.

Dates are (born, died) years. `_DAYS` adds the month and day where they are documented (curation's
"On this day", §5.9): Russian composers in the Gregorian (New Style) calendar, J. S. Bach's birth in the
Julian date Eisenach used (21 March; 31 March N.S.). A baptism stands in for an unrecorded birth date and is
labelled as one (Beethoven: baptised 17 December 1770). Disputed days (Chopin's and Verdi's births, Purcell,
Prokofiev's and Weber's births) are left out rather than guessed. Sources: the composers' Wikipedia articles
and Grove Music Online.
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import norm_key, surname


@dataclass(frozen=True, slots=True)
class KnownComposer:
    name: str
    born: int | None
    died: int | None
    mbid: str | None = None
    born_md: str | None = None       # "MM-DD"
    died_md: str | None = None
    baptised: bool = False           # born_md is the baptism date (birth day not recorded)


# name, born, died, extra spellings
_DATA: list[tuple[str, int, int | None, tuple[str, ...]]] = [
    ("Johann Sebastian Bach", 1685, 1750, ("J. S. Bach", "J.S. Bach", "JS Bach")),
    ("Carl Philipp Emanuel Bach", 1714, 1788, ("C. P. E. Bach", "C.P.E. Bach")),
    ("Ludwig van Beethoven", 1770, 1827, ("L. van Beethoven", "Beethoven, Ludwig van")),
    ("Wolfgang Amadeus Mozart", 1756, 1791, ("W. A. Mozart", "W.A. Mozart", "Mozart, Wolfgang Amadeus")),
    ("Joseph Haydn", 1732, 1809, ("Franz Joseph Haydn", "Haydn, Joseph")),
    ("Johannes Brahms", 1833, 1897, ("Brahms, Johannes",)),
    ("Franz Schubert", 1797, 1828, ("Schubert, Franz",)),
    ("Robert Schumann", 1810, 1856, ()),
    ("Clara Schumann", 1819, 1896, ()),
    ("Felix Mendelssohn", 1809, 1847, ("Felix Mendelssohn Bartholdy", "Felix Mendelssohn-Bartholdy")),
    ("Fanny Mendelssohn", 1805, 1847, ("Fanny Hensel",)),
    ("Frédéric Chopin", 1810, 1849, ("Fryderyk Chopin", "Frederic Chopin")),
    ("Franz Liszt", 1811, 1886, ()),
    ("Richard Wagner", 1813, 1883, ()),
    ("Giuseppe Verdi", 1813, 1901, ()),
    ("Giacomo Puccini", 1858, 1924, ()),
    ("Gioachino Rossini", 1792, 1868, ("Gioacchino Rossini",)),
    ("Gaetano Donizetti", 1797, 1848, ()),
    ("Vincenzo Bellini", 1801, 1835, ()),
    ("Antonín Dvořák", 1841, 1904, ("Antonin Dvorak",)),
    ("Bedřich Smetana", 1824, 1884, ("Bedrich Smetana",)),
    ("Leoš Janáček", 1854, 1928, ("Leos Janacek",)),
    ("Pyotr Ilyich Tchaikovsky", 1840, 1893, ("Peter Tchaikovsky", "P. I. Tchaikovsky", "Piotr Ilyich Tchaikovsky",
                                             "Pjotr Iljitsch Tschaikowski", "Peter Iljitsch Tschaikowsky")),
    ("Sergei Rachmaninoff", 1873, 1943, ("Sergei Rachmaninov", "Sergey Rachmaninov")),
    ("Sergei Prokofiev", 1891, 1953, ("Sergey Prokofiev",)),
    ("Dmitri Shostakovich", 1906, 1975, ("Dmitry Shostakovich", "DSCH")),
    ("Igor Stravinsky", 1882, 1971, ()),
    ("Nikolai Rimsky-Korsakov", 1844, 1908, ()),
    ("Modest Mussorgsky", 1839, 1881, ("Modest Musorgsky",)),
    ("Alexander Borodin", 1833, 1887, ()),
    ("Gustav Mahler", 1860, 1911, ()),
    ("Anton Bruckner", 1824, 1896, ()),
    ("Richard Strauss", 1864, 1949, ()),
    ("Johann Strauss II", 1825, 1899, ("Johann Strauss Jr.", "Johann Strauss (Sohn)")),
    ("Claude Debussy", 1862, 1918, ()),
    ("Maurice Ravel", 1875, 1937, ()),
    ("Gabriel Fauré", 1845, 1924, ("Gabriel Faure",)),
    ("Camille Saint-Saëns", 1835, 1921, ("Camille Saint-Saens",)),
    ("Hector Berlioz", 1803, 1869, ()),
    ("Georges Bizet", 1838, 1875, ()),
    ("Erik Satie", 1866, 1925, ()),
    ("César Franck", 1822, 1890, ("Cesar Franck",)),
    ("Jean Sibelius", 1865, 1957, ()),
    ("Edvard Grieg", 1843, 1907, ()),
    ("Carl Nielsen", 1865, 1931, ()),
    ("Edward Elgar", 1857, 1934, ("Sir Edward Elgar",)),
    ("Ralph Vaughan Williams", 1872, 1958, ("Vaughan Williams",)),   # the compound surname alone
    ("Benjamin Britten", 1913, 1976, ()),
    ("Gustav Holst", 1874, 1934, ()),
    ("Henry Purcell", 1659, 1695, ()),
    ("George Frideric Handel", 1685, 1759, ("Georg Friedrich Händel", "George Frederick Handel", "G. F. Handel")),
    ("Antonio Vivaldi", 1678, 1741, ()),
    ("Arcangelo Corelli", 1653, 1713, ()),
    ("Claudio Monteverdi", 1567, 1643, ()),
    ("Georg Philipp Telemann", 1681, 1767, ()),
    ("Johann Pachelbel", 1653, 1706, ()),
    ("Jean-Philippe Rameau", 1683, 1764, ()),
    ("Domenico Scarlatti", 1685, 1757, ()),
    ("Christoph Willibald Gluck", 1714, 1787, ()),
    ("Giovanni Pierluigi da Palestrina", 1525, 1594, ()),
    ("Thomas Tallis", 1505, 1585, ()),
    ("William Byrd", 1540, 1623, ()),
    ("Josquin des Prez", 1450, 1521, ()),
    ("Hildegard von Bingen", 1098, 1179, ()),
    ("Arnold Schoenberg", 1874, 1951, ("Arnold Schönberg",)),
    ("Alban Berg", 1885, 1935, ()),
    ("Anton Webern", 1883, 1945, ()),
    ("Béla Bartók", 1881, 1945, ("Bela Bartok",)),
    ("Zoltán Kodály", 1882, 1967, ("Zoltan Kodaly",)),
    ("Samuel Barber", 1910, 1981, ()),
    ("Aaron Copland", 1900, 1990, ()),
    ("George Gershwin", 1898, 1937, ()),
    ("Leonard Bernstein", 1918, 1990, ()),
    ("Charles Ives", 1874, 1954, ()),
    ("Philip Glass", 1937, None, ()),
    ("Steve Reich", 1936, None, ()),
    ("John Adams", 1947, None, ()),
    ("Arvo Pärt", 1935, None, ("Arvo Part",)),
    ("Olivier Messiaen", 1908, 1992, ()),
    ("Francis Poulenc", 1899, 1963, ()),
    ("Manuel de Falla", 1876, 1946, ()),
    ("Isaac Albéniz", 1860, 1909, ("Isaac Albeniz",)),
    ("Joaquín Rodrigo", 1901, 1999, ("Joaquin Rodrigo",)),
    ("Heitor Villa-Lobos", 1887, 1959, ()),
    ("Max Bruch", 1838, 1920, ()),
    ("Carl Maria von Weber", 1786, 1826, ()),
    ("Niccolò Paganini", 1782, 1840, ("Niccolo Paganini",)),
    ("Alexander Scriabin", 1872, 1915, ("Alexander Skryabin",)),
    ("Ottorino Respighi", 1879, 1936, ()),
    ("Pietro Mascagni", 1863, 1945, ()),
    ("Ruggero Leoncavallo", 1857, 1919, ()),
    ("Jules Massenet", 1842, 1912, ()),
    ("Charles Gounod", 1818, 1893, ()),
    ("Jacques Offenbach", 1819, 1880, ()),
    ("Engelbert Humperdinck", 1854, 1921, ()),
    ("Hugo Wolf", 1860, 1903, ()),
    ("Luigi Boccherini", 1743, 1805, ()),
    ("Tomaso Albinoni", 1671, 1751, ()),
    ("Giovanni Battista Pergolesi", 1710, 1736, ()),
    ("Gregorio Allegri", 1582, 1652, ()),
]

# name -> (birth or baptism "MM-DD", death "MM-DD"); a leading "~" marks a baptism date
_DAYS: dict[str, tuple[str | None, str | None]] = {
    "Johann Sebastian Bach": ("03-21", "07-28"),
    "Carl Philipp Emanuel Bach": ("03-08", "12-14"),
    "Ludwig van Beethoven": ("~12-17", "03-26"),
    "Wolfgang Amadeus Mozart": ("01-27", "12-05"),
    "Joseph Haydn": ("03-31", "05-31"),
    "Johannes Brahms": ("05-07", "04-03"),
    "Franz Schubert": ("01-31", "11-19"),
    "Robert Schumann": ("06-08", "07-29"),
    "Clara Schumann": ("09-13", "05-20"),
    "Felix Mendelssohn": ("02-03", "11-04"),
    "Fanny Mendelssohn": ("11-14", "05-14"),
    "Frédéric Chopin": (None, "10-17"),
    "Franz Liszt": ("10-22", "07-31"),
    "Richard Wagner": ("05-22", "02-13"),
    "Giuseppe Verdi": (None, "01-27"),
    "Giacomo Puccini": ("12-22", "11-29"),
    "Gioachino Rossini": ("02-29", "11-13"),
    "Gaetano Donizetti": ("11-29", "04-08"),
    "Vincenzo Bellini": ("11-03", "09-23"),
    "Antonín Dvořák": ("09-08", "05-01"),
    "Bedřich Smetana": ("03-02", "05-12"),
    "Leoš Janáček": ("07-03", "08-12"),
    "Pyotr Ilyich Tchaikovsky": ("05-07", "11-06"),
    "Sergei Rachmaninoff": ("04-01", "03-28"),
    "Sergei Prokofiev": (None, "03-05"),
    "Dmitri Shostakovich": ("09-25", "08-09"),
    "Igor Stravinsky": ("06-17", "04-06"),
    "Nikolai Rimsky-Korsakov": ("03-18", "06-21"),
    "Modest Mussorgsky": ("03-21", "03-28"),
    "Alexander Borodin": ("11-12", "02-27"),
    "Gustav Mahler": ("07-07", "05-18"),
    "Anton Bruckner": ("09-04", "10-11"),
    "Richard Strauss": ("06-11", "09-08"),
    "Johann Strauss II": ("10-25", "06-03"),
    "Claude Debussy": ("08-22", "03-25"),
    "Maurice Ravel": ("03-07", "12-28"),
    "Gabriel Fauré": ("05-12", "11-04"),
    "Camille Saint-Saëns": ("10-09", "12-16"),
    "Hector Berlioz": ("12-11", "03-08"),
    "Georges Bizet": ("10-25", "06-03"),
    "Erik Satie": ("05-17", "07-01"),
    "César Franck": ("12-10", "11-08"),
    "Jean Sibelius": ("12-08", "09-20"),
    "Edvard Grieg": ("06-15", "09-04"),
    "Carl Nielsen": ("06-09", "10-03"),
    "Edward Elgar": ("06-02", "02-23"),
    "Ralph Vaughan Williams": ("10-12", "08-26"),
    "Benjamin Britten": ("11-22", "12-04"),
    "Gustav Holst": ("09-21", "05-25"),
    "Henry Purcell": (None, "11-21"),
    "George Frideric Handel": ("02-23", "04-14"),
    "Antonio Vivaldi": ("03-04", "07-28"),
    "Arcangelo Corelli": ("02-17", "01-08"),
    "Claudio Monteverdi": ("~05-15", "11-29"),
    "Georg Philipp Telemann": ("03-14", "06-25"),
    "Johann Pachelbel": ("~09-01", None),
    "Jean-Philippe Rameau": ("~09-25", "09-12"),
    "Domenico Scarlatti": ("10-26", "07-23"),
    "Christoph Willibald Gluck": ("07-02", "11-15"),
    "Giovanni Pierluigi da Palestrina": (None, "02-02"),
    "Thomas Tallis": (None, "11-23"),
    "William Byrd": (None, "07-04"),
    "Josquin des Prez": (None, "08-27"),
    "Hildegard von Bingen": (None, "09-17"),
    "Arnold Schoenberg": ("09-13", "07-13"),
    "Alban Berg": ("02-09", "12-24"),
    "Anton Webern": ("12-03", "09-15"),
    "Béla Bartók": ("03-25", "09-26"),
    "Zoltán Kodály": ("12-16", "03-06"),
    "Samuel Barber": ("03-09", "01-23"),
    "Aaron Copland": ("11-14", "12-02"),
    "George Gershwin": ("09-26", "07-11"),
    "Leonard Bernstein": ("08-25", "10-14"),
    "Charles Ives": ("10-20", "05-19"),
    "Philip Glass": ("01-31", None),
    "Steve Reich": ("10-03", None),
    "John Adams": ("02-15", None),
    "Arvo Pärt": ("09-11", None),
    "Olivier Messiaen": ("12-10", "04-27"),
    "Francis Poulenc": ("01-07", "01-30"),
    "Manuel de Falla": ("11-23", "11-14"),
    "Isaac Albéniz": ("05-29", "05-18"),
    "Joaquín Rodrigo": ("11-22", "07-06"),
    "Heitor Villa-Lobos": ("03-05", "11-17"),
    "Max Bruch": ("01-06", "10-02"),
    "Carl Maria von Weber": (None, "06-05"),
    "Niccolò Paganini": ("10-27", "05-27"),
    "Alexander Scriabin": ("01-06", "04-27"),
    "Ottorino Respighi": ("07-09", "04-18"),
    "Pietro Mascagni": ("12-07", "08-02"),
    "Ruggero Leoncavallo": ("04-23", "08-09"),
    "Jules Massenet": ("05-12", "08-13"),
    "Charles Gounod": ("06-17", "10-18"),
    "Jacques Offenbach": ("06-20", "10-05"),
    "Engelbert Humperdinck": ("09-01", "09-27"),
    "Hugo Wolf": ("03-13", "02-22"),
    "Luigi Boccherini": ("02-19", "05-28"),
    "Tomaso Albinoni": ("06-08", "01-17"),
    "Giovanni Battista Pergolesi": ("01-04", "03-16"),
}

# Composers who are also prominent performers (conductors, pianists, violinists): an artist credit naming
# one of them is performer evidence first, so it never makes them the composer on its own (§5.11).
PERFORMING = frozenset({
    "Leonard Bernstein", "Benjamin Britten", "Igor Stravinsky", "Richard Strauss", "Edward Elgar",
    "Sergei Rachmaninoff", "Aaron Copland", "Gustav Mahler", "Franz Liszt", "Hector Berlioz",
})
# surnames shared by several notable composers outside this list (Leopold Mozart, Michael Haydn, Alessandro
# Scarlatti): a bare one resolves only with album context, never as "the only plausible match"
WORLD_AMBIGUOUS = frozenset({"mozart", "haydn", "scarlatti", "couperin", "gabrieli", "stamitz", "benda",
                             "graun", "boulanger", "berkeley", "adams", "williams", "weber", "berg", "franck",
                             "wolf", "byrd", "glass", "reich"})

_BY_KEY: dict[str, KnownComposer] = {}
_BY_SURNAME: dict[str, list[KnownComposer]] = {}
for _name, _born, _died, _alts in _DATA:
    _b, _d = _DAYS.get(_name, (None, None))
    _c = _BY_KEY.get(norm_key(_name)) or KnownComposer(_name, _born, _died, born_md=(_b or "").lstrip("~") or None,
                                                       died_md=_d, baptised=bool(_b and _b.startswith("~")))
    for _s in (_name, *_alts):
        _BY_KEY[norm_key(_s)] = _c
    _sk = norm_key(surname(_name, particles=False))
    if _c not in _BY_SURNAME.setdefault(_sk, []):
        _BY_SURNAME[_sk].append(_c)


def lookup(name: str | None) -> KnownComposer | None:
    """Exact (normalized) full-name match. Bare surnames and partial names abstain."""
    if not name:
        return None
    return _BY_KEY.get(norm_key(name))


def find_in_credit(artist: str | None) -> KnownComposer | None:
    """The one known composer among an artist credit's names ('Johannes Brahms; Some Orchestra')."""
    if not artist:
        return None
    import re

    hits = {c.name: c for part in re.split(r"\s*(?:;|/|,|&|\band\b|\x00)\s*", artist) if (c := lookup(part))}
    return next(iter(hits.values())) if len(hits) == 1 else None


def by_surname(name: str | None) -> list[KnownComposer]:
    """Every listed composer with this surname ('Strauss' -> Johann II and Richard)."""
    return list(_BY_SURNAME.get(norm_key(surname(name or "", particles=False)), [])) if name else []


def all_known() -> list[KnownComposer]:
    return list(dict.fromkeys(_BY_KEY.values()))
