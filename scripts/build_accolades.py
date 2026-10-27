"""Rebuild classical_core/data/accolades.json from Wikipedia wikitext and the BBC Music Magazine Awards winner
pages (docs/accolades.md).

    python scripts/build_accolades.py WIKI_DIR [OUT]          # parse saved pages (WIKI_DIR/<Page>.wiki, WIKI_DIR/bbc/)
    python scripts/build_accolades.py --fetch WIKI_DIR [OUT]  # first download the pages (one request a second)

Facts only: award, category, year, result, composer, work, performers (and a label when the list names one).
No prose is copied. Entry ids are sha1 hashes of those facts, so a rebuild keeps the ids user decisions refer to.
The parsers are deliberately simple; rows they cannot read cleanly are dropped, never guessed.
"""

from __future__ import annotations

import collections
import hashlib
import html as htmllib
import json
import pathlib
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

COMPILED = "2026-10-27"
GRAMMY_PAGES = {
    "Grammy_Award_for_Best_Opera_Recording": "Best Opera Recording",
    "Grammy_Award_for_Best_Orchestral_Performance": "Best Orchestral Performance",
    "Grammy_Award_for_Best_Choral_Performance": "Best Choral Performance",
    "Grammy_Award_for_Best_Classical_Album": "Best Classical Album",
    "Grammy_Award_for_Best_Chamber_Music_Small_Ensemble_Performance": "Best Chamber Music/Small Ensemble Performance",
    "Grammy_Award_for_Best_Classical_Instrumental_Solo": "Best Classical Instrumental Solo",
    "Grammy_Award_for_Best_Classical_Solo_Vocal_Album": "Best Classical Solo Vocal Album",
}
# the article title when it differs from the saved file name ('/' cannot be in a file name)
ARTICLE = {"Grammy_Award_for_Best_Chamber_Music_Small_Ensemble_Performance":
           "Grammy_Award_for_Best_Chamber_Music/Small_Ensemble_Performance"}
GRAMOPHONE = "Gramophone_Classical_Music_Awards"
DIAPASON = "Diapason_d'Or"
WP = "https://en.wikipedia.org/wiki/"
SOURCES = [
    {"id": "wp-grammy", "title": "Wikipedia: Grammy classical category articles (Best Opera Recording, Best "
     "Orchestral Performance, Best Choral Performance, Best Classical Album, Best Chamber Music/Small Ensemble "
     "Performance, Best Classical Instrumental Solo, Best Classical Solo Vocal Album)",
     "url": WP + "Grammy_Award_for_Best_Opera_Recording", "retrieved": COMPILED,
     "licence": "facts only (award, year, work, performers); no prose copied"},
    {"id": "wp-gramophone", "title": "Wikipedia: Gramophone Classical Music Awards", "url": WP + GRAMOPHONE,
     "retrieved": COMPILED, "licence": "facts only"},
    {"id": "wp-diapason", "title": "Wikipedia: Diapason d'Or (Diapason d'Or de l'Année lists)",
     "url": WP + "Diapason_d%27Or", "retrieved": COMPILED, "licence": "facts only"},
    {"id": "bbc-music-magazine", "title": "BBC Music Magazine Awards: the magazine's own winner announcements "
     "(classical-music.com; 2007-2011 and 2016-2018 as archived by the Internet Archive). Each entry carries "
     "the page it was read from", "url": "https://www.classical-music.com/awards", "retrieved": COMPILED,
     "licence": "facts only (award, year, work, performers, label); no review text copied"},
]
SRC = {"Grammy": 0, "Gramophone Award": 1, "Diapason d'Or de l'Année": 2, "BBC Music Magazine Award": 3}
# Gramophone categories that are not a recording of a work
SKIP = re.compile(r"artist|lifetime|label|achievement|orchestra of the year|engineering|best-selling|young|concept|"
                  r"video|dvd|music theatre|special|piano$", re.IGNORECASE)
ABOUT = ("Classical award winners and Grammy nominations, compiled from the public lists named in `sources` "
         "(facts only: award, year, composer, work, performers; no review text). Grammy years are ceremony years "
         "(the release is one to two years earlier); Gramophone, Diapason and BBC years are award years. BBC entries "
         "carry the page they were read from as a ninth column (the label column is then null when the page names "
         "none). Rebuilt by the "
         "parser described in docs/accolades.md; entry ids are stable hashes, so user decisions survive a rebuild.")


def file_name(page: str) -> str:
    return page.replace("'", "_27") + ".wiki"


def fetch(wiki: pathlib.Path) -> None:
    wiki.mkdir(parents=True, exist_ok=True)
    for page in [*GRAMMY_PAGES, GRAMOPHONE, DIAPASON]:
        title = ARTICLE.get(page, page)
        url = "https://en.wikipedia.org/w/index.php?" + urllib.parse.urlencode({"title": title, "action": "raw"})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            (wiki / file_name(page)).write_bytes(r.read())
        time.sleep(1.1)
    fetch_web(wiki)


# ---- wikitext ------------------------------------------------------------------------------------------------


def unlink(s: str, abbr: bool = True, italics: bool = True, tags: bool = False) -> str:
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<ref.*?</ref>", "", s, flags=re.DOTALL)
    if abbr:
        s = re.sub(r"\{\{abbr\|([^|}]*)\|[^}]*\}\}", r"\1", s)
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    if tags:
        s = re.sub(r"<[^>]+>", "", s)
    if italics:
        s = s.replace("''", "")
    return re.sub(r"\s+", " ", s).strip()


def grammy_unlink(s: str) -> str:
    return unlink(s, abbr=False, italics=False, tags=True)


def split_work(t: str) -> tuple[str | None, str]:
    t = t.strip().strip("'\"").strip()
    if ":" in t:
        c, w = t.split(":", 1)
        if len(c) < 40:
            return c.strip(), w.strip()
    return None, t


def grammy_people(s: str) -> list[str]:
    # a role in brackets ends a name ('Karajan (conductor) Franco Corelli' lacks the comma in the source)
    s = re.sub(r"\((?:conductors?|cd) of (?:the )?([^()]*)\)", r", \1", s)
    s = re.sub(r"\((?:conductors?|cd|soloists?|main artist|ensembles?|orchestra|accompanists?|pianist|"
               r"chorus masters?|choirs?|chorus(?:es)?|choir directors?|chorus director|musician|mixer)\)", ",", s)
    s = re.sub(r"\(\s*with\s+(?:the\s+)?([^()]*)\)", r", \1", s)
    s = re.sub(r"[;,]?\s*[^;,]*?,\s*producers?\b", "", s)
    s = re.sub(r"[;,]?\s*[^;,()]*\((?:[^)]*producer[^)]*|[^)]*engineer[^)]*)\)", "", s)
    s = re.sub(r"\((?:producers?|conductor|engineer[^)]*|soloists?|[^)]*producer[^)]*)\)", "", s)
    out = []
    for p in re.split(r",|;|&| and | conducting (?:the )?", s):
        p = re.sub(r"\b(conductor|producers?|choir director|chorus master)\b", "", p).strip(" .-–")
        if p.count("(") != p.count(")"):  # a bracket split across names is noise, not part of a name
            p = re.sub(r"\(\s*(?:conductors?|cd)?\s*|\)", "", p).strip()
        if p and len(p) < 80:
            out.append(p)
    return out


def parse_grammy(wiki: pathlib.Path) -> list[dict]:
    res = []
    for page, cat in GRAMMY_PAGES.items():
        txt = (wiki / file_name(page)).read_text()
        for row in re.split(r"\n\|-", txt):
            m = re.search(r"Grammy Awards\|(\d{4})\]\]", row)
            if not m:
                continue
            year, body = int(m.group(1)), row[m.end():]
            end = body.find("\n|}")                      # the last row runs into the table's end
            body = body[:end] if end >= 0 else body
            sd = body.find("{{smalldiv")
            if sd < 0:                                    # newer rows: the nominees are a plain bullet list
                bm = re.search(r"\n\s*\*", body)
                sd = bm.start() if bm else -1
            head, noms = (body[:sd], body[sd:]) if sd >= 0 else (body, "")
            cells = [c.strip() for c in re.split(r"\n\|", head) if c.strip() and not c.strip().startswith("style")]
            work_i = next((i for i, c in enumerate(cells) if re.match(r"^\s*(''|\")", c)), None)
            if work_i is None:
                continue
            wc = grammy_unlink(cells[work_i])
            extra: list[str] = []
            pm = re.match(r"^(.*'')\s*\(([^()]*)\)\s*$", wc)   # ''Smyth: The Prison'' (Experiential Chorus ...)
            if pm:
                wc, extra = pm.group(1), grammy_people(pm.group(2))
            comp, work = split_work(wc)
            perf = [p for i, c in enumerate(cells) if i != work_i for p in grammy_people(grammy_unlink(c))] + extra
            res.append({"award": "Grammy", "category": cat, "year": year, "result": "winner", "composer": comp, "work": work,
                            "performers": perf})
            for line in re.findall(r"\n\s*\*(.*)", noms):
                line = grammy_unlink(line).rstrip("}")
                mm = (re.match(r"(.*?)\s+(?:–|-|for)\s+(?:''|\")(.*?)(?:''|\")\s*(?:\((.*)\))?\s*$", line)
                      or re.match(r"(.*?)\s+(?:–|for)\s+(.*?)\s*(?:\((.*)\))?\s*$", line))
                if not mm:
                    continue
                comp, work = split_work(mm.group(2))
                perf = grammy_people(mm.group(1)) + (grammy_people(mm.group(3)) if mm.group(3) else [])
                res.append({"award": "Grammy", "category": cat, "year": year, "result": "nominee", "composer": comp, "work": work,
                                "performers": perf})
    return res


def parse_gramophone(wiki: pathlib.Path) -> list[dict]:
    txt = (wiki / file_name(GRAMOPHONE)).read_text()
    res = []
    txt = re.sub(r"\n(?=\|\|)", " ", txt)          # a row continued on the next line ('|| ...')
    for sec in re.split(r"\n==[^=]", txt)[1:]:
        hm = re.search(r"\n! Category(.*)", sec)
        if not hm:
            continue
        years = [re.match(r"\s*(\d{4})", unlink(y)).group(1) for y in hm.group(1).split("!!")[1:]]
        for line in re.findall(r"\n\|\s*([^-\n].*)", sec):
            cells = [c.strip() for c in line.split("||")]
            cat = unlink(cells[0])
            for y, cell in zip(years, cells[1:]):
                c = unlink(cell)
                m = re.match(r"(.*?)\s+[–-]\s+([^:]{2,40}?):\s*(.*)$", c)
                if not c or c in ("—", "-", "N/A") or not m:
                    continue
                perf = [p.strip(" .") for p in re.split(r",| conducting (?:the )?| and |&", m.group(1)) if p.strip(" .")]
                work, sub = m.group(3), None
                mm = re.search(r"\(([^()]*)\)\s*$", work)
                if cat.lower().startswith("record") and mm:
                    sub, work = mm.group(1), work[:mm.start()].strip()
                res.append({"award": "Gramophone Award", "category": cat + (f" ({sub})" if sub else ""), "year": int(y),
                                "result": "winner", "composer": m.group(2).strip(), "work": work.strip(), "performers": perf})
    return res


MEDIA = r"\((?:\d+\s+)?(?i:CD|SACD|DVD|Blu-ray)[^)]*\)\.?"


def diapason_new(line: str, year: int) -> list[dict]:
    """From 2014 a line is a category, then one or more 'Composer: Work, performers. Label (CD).' awards.
    Performers are the comma parts from the first one with a link or a '(role)'; a part before them is the work."""
    m = re.match(r"[^:\[]{2,60}:\s*(.*)$", line)
    if not m:
        return []
    out = []
    body = re.sub(MEDIA, lambda x: x.group(0).rstrip(".") + "\x00", m.group(1))
    # a new award also starts at 'Composer:' after a full stop (the later years give no label)
    body = re.sub(r"\.\s+(?=(?:\[\[)?[A-ZÀ-Ž][^,:.()\[\]\x00]{1,40}(?:\]\])?\s?:)", ".\x00", body)
    for seg in body.split("\x00"):
        seg = seg.strip()
        label = None
        lm = re.search(r"\.\s+([^.,\[\]]{2,40}?)\s*" + MEDIA + r"\s*$", seg)
        if lm:
            label, seg = lm.group(1), seg[:lm.start()]
        seg = re.sub(MEDIA + r"\s*$", "", seg).strip(" .")
        cm = re.match(r"([A-ZÀ-Ž][^,:\[\]\"]{1,40}?)\s*(?::|\s[–-]\s)\s*(.*)$", seg)
        if not cm:
            continue
        parts = [p.strip() for p in re.split(r",\s+|;\s+|\.\s+(?=\[\[)", cm.group(2)) if p.strip()]
        first = next((i for i, p in enumerate(parts) if "[[" in p or re.search(r"\([a-z]", p)), None)
        if not first or any(":" in p for p in parts[first:]):  # no performer, or a second award run in: unsure
            continue
        parts[first] = re.sub(r"^Artists include\s+", "", parts[first])
        out.append({"award": "Diapason d'Or de l'Année", "category": None, "year": year, "result": "winner",
                    "composer": cm.group(1).strip(), "work": unlink(", ".join(parts[:first])),
                    "performers": [unlink(re.sub(r"^Sir\s+", "", p)) for p in parts[first:]], "label": label})
    return out


def parse_diapason(wiki: pathlib.Path) -> list[dict]:
    res, year = [], None
    for line in (wiki / file_name(DIAPASON)).read_text().splitlines():
        m = re.match(r"==\s*Diapason d'Or de l'ann[ée]e (\d{4})", line, re.IGNORECASE)
        if m:
            year = int(m.group(1))
            continue
        if line.startswith("=="):
            year = None
            continue
        if not (year and line.startswith("*")):
            continue
        if year >= 2014:  # 'Category: Composer: Work, performers. Label (CD). Next award ...'
            res += diapason_new(line.lstrip("* "), year)
            continue
        s = re.sub(r"^(DVD|Historical|Concerto Award)\s*[–-]?\s*", "", unlink(line[1:]).strip())
        m = re.match(r"(.{3,120}?)\s*[:;]\s*(.*)$", s)
        if not m:
            continue
        perf_s, rest = m.groups()
        cm = re.match(r"\s*([A-ZÀ-Ž][^,:.]{1,40}?)\s*(?:[:,]|\s+-\s+)\s*(.*)$", rest)
        if not cm:
            continue
        comp, work = cm.groups()
        perf = [p.strip(" .") for p in re.split(r",| and |&", perf_s) if p.strip(" .")]
        # what follows the work is more performers, then the label
        parts = [p.strip() for p in re.split(r"\.\s+|,\s+", work) if p.strip()]
        res.append({"award": "Diapason d'Or de l'Année", "category": None, "year": year, "result": "winner",
                        "composer": comp.strip(), "work": parts[0] if parts else work, "performers": perf + parts[1:-1],
                        "label": parts[-1] if len(parts) > 1 else None})
    return res


# ---- BBC Music Magazine Awards ---------------------------------------------------------------------------------
# The awards' own winner lists on classical-music.com (BBC Music Magazine's site), live or, for the older
# years, as archived by the Wayback Machine. There is no Wikipedia list. Pages come in four kinds:
#   list      a winners page: a heading per category, then the winning disc (composer, work, performers, label)
#   one       a page for one category's winner (2008, 2009)
#   nominees  one category's nominees (2014 has all categories on one page); the winner is the nominee the
#             year's 'announce' page names in the sentence about that category
#   announce  the winners announcement, in prose (used only to pick among the nominees)
# Years with no list, no category pages and no nominee list (2012, 2013, 2015) are left out.

BBC = "BBC Music Magazine Award"
CM = "https://www.classical-music.com/"
BMM = "http://www.bbcmusicmagazine.com/"
UA = "classical-music-assistant accolades build (https://github.com/GoatsAndMonkeys/sethster)"
# (year, kind, category, url, Wayback timestamp or None for the live page)
_W08 = {"Orchestral": ("orchestwinner08", "20080622041600"), "Opera": ("operawinner08", "20080622041549"),
        "Chamber": ("chamberwinner08", "20080622041508"), "Choral": ("choralwinner08", "20080513092534"),
        "Instrumental": ("instrumwinner08", "20080513092539"), "Vocal": ("vocalwinner08", "20080513092544"),
        "Newcomer": ("newcomerwin08", "20080622041534"), "Premiere": ("premwinner08", "20080514111116")}
_W09 = {"Recording of the Year": ("disc-year-orchestral", "20100202042637"),
        "Opera": ("opera", "20101206082447"), "Chamber": ("chamber", "20100202042633"),
        "Choral": ("choral", "20101206082436"), "Instrumental": ("instrumental", "20100202042713"),
        "Vocal": ("vocal", "20101206082126"), "Newcomer": ("newcomer", "20101206082111"),
        "Premiere": ("premiere", "20101206082116")}
BBC_PAGES: list[tuple[int, str, str | None, str, str | None]] = [
    (2007, "list", None, BMM + "awards/awards2007_winners.asp", "20081106214817"),
    *[(2008, "one", c, BMM + f"awards/{p}.asp", t) for c, (p, t) in _W08.items()],
    *[(2009, "one", c, BMM + f"awards-2009-{p}-winner", t) for c, (p, t) in _W09.items()],
    (2010, "list", None, "http://www.classical-music.com/sites/default/files/winners.html", "20110708163725"),
    (2011, "list", None, "http://www.classical-music.com/awards2011", "20111017074237"),
    (2012, "list", None, CM + "news/bbc-music-magazine-awards-2012-winners-announced", None),
    (2014, "list", None, CM + "news/bbc-music-magazine-awards-2014-winners-announced", None),
    (2015, "list", None, CM + "news/winners-bbc-music-magazine-awards-2015-announced", None),
    (2016, "list", None, "http://awards.classical-music.com/2016/winners", "20191205172014"),
    (2017, "list", None, "http://awards.classical-music.com/2017/winners", "20191205172850"),
    (2018, "list", None, "http://awards.classical-music.com/2018/winners", "20191205181349"),
    (2019, "list", None, CM + "articles/bbc-music-magazine-awards-2019-0", None),
    (2020, "list", None, CM + "news/winners-2020-bbc-music-magazine-awards", None),
    (2021, "list", None, CM + "articles/the-winners-of-the-2021-bbc-music-magazine-awards", None),
    (2022, "nominees", None, CM + "news/bbc-music-magazine-announces-nominees-for-its-2022-awards", None),
    (2022, "announce", None, CM + "news/bbc-music-magazine-award-winners-announced", None),
    (2023, "nominees", None, CM + "news/bbc-music-magazine-announces-nominees-for-its-2023-awards", None),
    (2023, "announce", None, CM + "awards/2023-awards/bbc-music-awards-2023-winners-announced", None),
    (2024, "list", None, CM + "awards/2024-awards/bbc-music-magazine-awards-2024-winners-announced", None),
    (2025, "list", None, CM + "awards/2025-awards/bbc-music-awards-2025-winners-announced", None),
    (2026, "list", None, CM + "awards/2026-awards/bbc-music-awards-2026-winners-announced", None),
]


def page_file(year: int, kind: str, cat: str | None) -> str:
    return f"bbc/{year}-{kind}" + (f"-{cat.lower().replace(' ', '-')}" if cat else "") + ".html"


def page_url(url: str, wayback: str | None) -> str:
    """Where the page is read from (the Wayback copy for the older years) and cited."""
    return f"https://web.archive.org/web/{wayback}/{url}" if wayback else url


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_web(cache: pathlib.Path) -> None:
    """Download the BBC (and ICMA) pages not yet cached, at most one request a second."""
    for year, kind, cat, url, wb in BBC_PAGES:
        f = cache / page_file(year, kind, cat)
        if f.exists() and f.stat().st_size:
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        # 'id_' asks the Wayback Machine for the page as archived, without its toolbar
        body = http_get(f"https://web.archive.org/web/{wb}id_/{url}" if wb else url)
        time.sleep(1.1)
        if b"Temporarily Offline" in body[:5000] or len(body) < 2000:
            sys.exit(f"{url}: the archive did not return the page; run --fetch again later")
        f.write_bytes(body)


_BLOCK_TAGS = re.compile(r"<(?:/?(?:p|div|li|ul|ol|tr|td|th|table|section|article|header|footer|figure|h[1-6])"
                         r"|br)\b[^>]*>", re.IGNORECASE)


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def html_lines(html: str) -> list[tuple[str, str]]:
    """The page as lines of text, each with its style: 'head' (a heading, or all bold), 'em' (all italic), ''."""
    s = re.sub(r"<(script|style|noscript|svg|iframe|audio|template|nav)\b.*?</\1\s*>|<!--.*?-->", " ", html,
               flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<h[1-6]\b[^>]*>", "\n\x01", s, flags=re.IGNORECASE)
    s = re.sub(r"</h[1-6]\s*>", "\x02\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<(?:strong|b)\b[^>]*>", "\x01", s, flags=re.IGNORECASE)
    s = re.sub(r"</(?:strong|b)\s*>", "\x02", s, flags=re.IGNORECASE)
    s = re.sub(r"<(?:em|i)\b[^>]*>", "\x03", s, flags=re.IGNORECASE)
    s = re.sub(r"</(?:em|i)\s*>", "\x04", s, flags=re.IGNORECASE)
    s = _BLOCK_TAGS.sub("\n", s)
    s = htmllib.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").replace("\u200b", "")
    out = []
    bold = ital = 0
    for raw in s.split("\n"):
        plain = bolded = italic = 0
        for ch in raw:
            if ch == "\x01":
                bold += 1
            elif ch == "\x02":
                bold = max(0, bold - 1)
            elif ch == "\x03":
                ital += 1
            elif ch == "\x04":
                ital = max(0, ital - 1)
            elif not ch.isspace():
                if bold:
                    bolded += 1
                elif ital:
                    italic += 1
                else:
                    plain += 1
        text = re.sub(r"\s+", " ", re.sub(r"[\x01-\x04]", "", raw)).strip()
        if text:
            out.append((text, "head" if bolded and not plain and not italic
                        else "em" if italic and not plain and not bolded else ""))
    return out


def fold_text(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


BBC_CATEGORIES = {"roty": "Recording of the Year", "orchestral": "Orchestral", "concerto": "Concerto",
                  "opera": "Opera", "choral": "Choral", "chamber": "Chamber", "vocal": "Vocal",
                  "instrumental": "Instrumental", "premiere": "Premiere", "newcomer": "Newcomer"}
# awards that are not for a recording of a work, or not in the list above: skipped
BBC_OTHER = re.compile(r"^(?:dvd|dvd .*|.* dvd|film|technical excellence|personality|app|song|lifetime .*|"
                       r"special .*|.*\(performance\)|.*\(documentary\))$")


def bbc_category(line: str) -> list[str] | None:
    """The categories a heading names ('Orchestral & Disc of the year' -> two), [] for an award that is not
    kept (DVD, Personality), None when the line is not a category heading."""
    s = fold_text(line).lower().strip(" *:.")
    s = re.sub(r"^.*?\bawards?\s+\d{4}\s*[-–:]\s*", "", s)
    s = re.sub(r"^(?:\d+\.\s*|jury awards?\s*:?\s*)", "", s)
    if not s or len(s) > 70:
        return None
    s = re.sub(r"\b(?:recording|disc) of the year\b", "roty", s)
    s = re.sub(r"\b(?:of the year|in recording|recording|nominations|bbc music magazine|best)\b", " ", s)
    s = re.sub(r"\b(?:awards?|winners?|category|prize)\b", " ", s)
    cats = []
    for piece in re.split(r"\s*(?:&|/|,|\+|\band\b)\s*", re.sub(r"\s+", " ", s).strip()):
        if piece in BBC_CATEGORIES:
            cats.append(BBC_CATEGORIES[piece])
        elif not BBC_OTHER.match(piece):
            return None
    return cats


def is_category_line(text: str, style: str) -> list[str] | None:
    """A heading, a capitalised line or one ending in 'Award' / 'of the Year' that names categories."""
    if style == "head" or text.isupper() or re.search(r"(?i)\b(?:award|awards|of the year|winner)\s*:?$", text):
        return bbc_category(text)
    return None


_NOISE = re.compile(r"^(?:winners?|you will need adobe.*|your browser .*|click here.*|audio clip.*|reviewed .*|"
                    r"buy this disc.*|listen\b.*|excerpt\b.*|what (?:the|our) .*|watch\b.*|read (?:more|our).*|"
                    r"prestoclassical.*|.* says:.*|.*\bsays:?$|to read the full .*|vote\b.*|"
                    r"listen to this nomination.*|buy from .*|buy this .*|related links|presented by .*)$",
                    re.IGNORECASE)
_END = re.compile(r"^(?:also nominated:?|the nominees:?|the other nominees.*|visit other bbc sites.*|"
                  r"facebook|privacy policy|current issue|jury awards?|you may also like|share this.*)$",
                  re.IGNORECASE)
_MEDIA = r"\s*\((?:[^()]*\b(?:discs?|CDs?|SACD|DVD|Blu-ray|download|LP)\b[^()]*)\)"
_WORDISH = re.compile(r"\b(?:symphon\w*|concert\w*|sonat\w*|quartets?|quintets?|trios?|songs?|lieder|works?|"
                      r"music|arias?|preludes?|etudes?|études?|suites?|variations|cantatas?|masses|mass|"
                      r"opera|vol|volume|piano|violin|cello|string|orchestral|chamber|choral|complete|"
                      r"requiem|vespers|in|no|nos|op)\b\.?", re.IGNORECASE)


def is_label_line(text: str, style: str) -> bool:
    """'Signum SIGCD589', 'Naïve V 5174', 'Profil Hänssler PH 09004 (hybrid CD/SACD, 3 discs)'."""
    t = re.sub(r"\s*[-–]\s*Reviewed .*$", "", re.sub(_MEDIA, "", text, flags=re.IGNORECASE)).strip(" .")
    if not re.search(r"\d", t) or len(t) > 70 or re.search(r"[;:]|\(\s*[a-z]|/\s*[A-ZÀ-Þ][a-zà-ÿ]", t):
        return bool(style == "em" and re.search(r"\d", t) and len(t) < 70
                    and not re.search(r"/\s*[A-ZÀ-Þ][a-zà-ÿ]", t))
    if re.search(r"\b(?:No|Nos|Op|Opp|Vol|BWV|K|D|Symphon\w*|Concert\w*|Sonat\w*|Quartets?|Preludes)\b\.?\s*\d", t):
        return False
    return bool(re.fullmatch(r"[A-ZÀ-Þ][\w'’&.!\- ]{0,40}?[\s.\-]*[\w.\-/ ]*\d[\w.\-/ ()+,&]*", t))


def label_text(text: str) -> str:
    return re.sub(r"\s*[-–]\s*Reviewed .*$", "", re.sub(_MEDIA, "", text, flags=re.IGNORECASE)).strip(" .")


def bbc_sections(lines: list[tuple[str, str]]) -> list[tuple[list[str], str | None, list[tuple[str, str]]]]:
    """[(categories, the heading just before the category line, the lines under it up to 'Also nominated' or
    the next category)]."""
    out: list[tuple[list[str], str | None, list[tuple[str, str]]]] = []
    cur: list[tuple[str, str]] | None = None
    prev: tuple[str, str] = ("", "")
    for text, style in lines:
        cats = is_category_line(text, style)
        if cats is not None:
            cur = []
            out.append((cats, prev[0] if prev[1] == "head" else None, cur))
        elif _END.match(text):
            cur = None
        elif cur is not None:
            cur.append((text, style))
        prev = (text, style)
    return out


def winner_block(lines: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str | None]:
    """The first disc under a heading: its lines up to its label line (the label is returned apart)."""
    out: list[tuple[str, str]] = []
    for text, style in lines:
        if _NOISE.match(text):
            continue
        if len(text) > 300:                       # review prose: the disc, if any, is above it
            if out:
                break
            continue
        if is_label_line(text, style):
            if out:
                return out, label_text(text)
            continue
        m = re.match(r"^([A-Z][A-Z &]+[A-Z])\s+[A-ZÀ-Þ][a-zà-ÿ]", text)
        if m and out and bbc_category(m.group(1)) is not None:     # 'PERSONALITY OF THE YEAR Anna Lapwood'
            break
        out.append((text, style))
    return out, None


_NAME = r"[A-ZÀ-Þ][^:;()\[\]\d]{0,60}?"
_COMP_SEP = re.compile(r"\s*(?:•|\*|·|/|&|,|\band\b)\s*")


def name_like(s: str) -> bool:
    """'Shostakovich', 'JS Bach', 'Louise Bertin', 'Rodgers & Hammerstein', 'Fauré • Franck'."""
    s = s.strip()
    if not s or re.search(r"\d|[\"“”'‘’]\s*$|^[\"“'‘]", s) or len(s) > 70:
        return False
    for piece in _COMP_SEP.split(s):
        if not piece:
            continue
        words = piece.split()
        if not 1 <= len(words) <= 4 or _WORDISH.search(piece) or not piece[0].isupper():
            return False
        if any(w[0].islower() and w not in ("de", "van", "von", "da", "di", "del", "der", "le", "la", "du")
               for w in words):
            return False
    return True


def composer_split(line: str) -> tuple[str, str] | None:
    """'Shostakovich: Symphony No. 7', 'Sibelius - Kullervo' -> (composer(s), work)."""
    m = re.match(r"^(" + _NAME + r")\s*:\s*(.+)$", line) or re.match(r"^(" + _NAME + r")\s+[-–]\s+(.+)$", line)
    if m and name_like(m.group(1)):
        return m.group(1).strip(), m.group(2).strip()
    return None


def all_caps_name(s: str) -> str:
    return " ".join(w.capitalize() if w.isupper() and len(w) > 2 else w for w in s.split()) if s.isupper() else s


# record labels, to take a label off the end of a performer line ('Sinfonia of London/John Wilson Chandos CHSA 5291')
BBC_LABELS = ("Alpha Classics", "Alpha", "Avanti Classics", "Avie", "BIS", "BR Klassik", "Bru Zane", "Champs Hill Records",
              "Chandos", "Channel Classics", "Decca", "Delphian", "Deutsche Grammophon", "DG", "DUX", "ECM", "EMI",
              "Erato", "Harmonia Mundi", "Hyperion", "Linn Records", "Linn", "LSO Live", "Myrios Classics", "Naïve",
              "NMC", "Nimbus", "Onyx", "Ondine", "Orfeo", "Pentatone", "Platoon Music", "Resonus", "Rubicon",
              "Signum Classics", "Signum Records", "Signum", "Sony Classical", "Sony", "Supraphon", "Virgin Classics",
              "Virgin", "Warner Classics", "Warner")
_LABEL_END = re.compile(r"^(.*\S)\s+((?:" + "|".join(map(re.escape, BBC_LABELS)) + r")\s+[A-Z]{2,}[\w.\- ]*\d[\w.\-()]*)$")
_COMP_LIST = re.compile(r"^((?:[A-Z]\.?\s)?[A-ZÀ-Þ][\w'-]+(?:\s*[•·]\s*(?:[A-Z]\.?\s)?[A-ZÀ-Þ][\w'-]+)+)\s+(.+)$")


def surname(name: str) -> str:
    return fold_text(name.split()[-1]).casefold() if name.split() else ""


def known_prefix(t: str, composers: frozenset[str]) -> tuple[list[str], str] | None:
    """'Smetana Má vlast', 'C Schumann • R Schumann Works for Oboe' -> (composers, work)."""
    m = _COMP_LIST.match(t)
    if m:
        return [c for c in _COMP_SEP.split(m.group(1)) if c], m.group(2)
    words = t.split()
    surnames = {surname(c) for c in composers}
    k = next((k for k in (3, 2, 1) if len(words) > k and (" ".join(words[:k]) in composers or (
        surname(words[k - 1]) in surnames and all(w[0].isupper() for w in words[:k])
        and not words[k - 1].isupper()))), None)                    # not a 'PAVEL HAAS QUARTET' heading
    return ([" ".join(words[:k])], " ".join(words[k:])) if k else None


def bbc_disc(lines: list[tuple[str, str]], label: str | None, artist: str | None = None,
             composers: frozenset[str] = frozenset()) -> dict | None:
    """One disc: {'composers': [...], 'work': str, 'performers': str, 'label': str | None}, or None when the
    lines cannot be taken apart cleanly. `composers` (known composer names) splits 'Smetana Má vlast' and
    'Choir/Sir Andrew Davis Elgar: Dream of Gerontius'."""
    texts, styles = [], []
    for t, st in lines:
        t = re.sub(r"(?<!\s)[*^†]+|[*^†]+(?!\s)", "", t)              # 'Flute Concerto*', '*Ulla Miilmann'
        t = re.sub(r"(?<=[a-z])- (?=[A-Z][a-z])", "-", t).strip(" ;,")  # 'Myung- Whun Chung'
        m = None if composer_split(t) else re.match(r"^(.*\S)\s+((?:[A-Z]\w*\s+)?[A-ZÀ-Þ][\w'-]+)\s*:\s*(.+)$", t)
        if m and m.group(2) in composers:                              # '...Sir Andrew Davis Elgar: Dream of'
            texts += [m.group(1), f"{m.group(2)}: {m.group(3)}"]
            styles += [st, st]
        elif t:
            texts.append(t)
            styles.append(st)
    if artist:
        key = fold_text(artist).casefold()
        while texts and fold_text(texts[0]).casefold() == key and len(texts) > 2:
            texts, styles = texts[1:], styles[1:]
    if len(texts) > 2 and not composer_split(texts[0]):                # 'Patricia Petibon' ... '(soprano)'
        key = fold_text(texts[0]).casefold().rstrip(". ")
        if any(fold_text(x).casefold().rstrip(". ") == key or fold_text(x).casefold().startswith(key + " (")
               or (key + " (") in fold_text(x).casefold() for x in texts[1:]):
            texts, styles = texts[1:], styles[1:]
    texts = [re.sub(r"(?<=\))\s*(?=[A-ZÀ-Þ])", "; ", t) for t in texts]    # '(cello) Staatskapelle' -> '(cello); St'
    if not label:
        for i, t in enumerate(texts):
            m = _LABEL_END.match(t)
            if t in BBC_LABELS or (m and i > 0):
                label = t if t in BBC_LABELS else m.group(2)
                texts[i] = "" if t in BBC_LABELS else m.group(1)
        styles = [st for t, st in zip(texts, styles) if t]
        texts = [t for t in texts if t]
    if not label and texts:
        m = re.match(r"^(.*\S)\s*\(([^()]{2,40})\)$", texts[0]) if composer_split(texts[0]) else None
        m = m or re.match(r"^(.*\S)\s*\(([^()]{2,40})\)$", texts[-1])
        if m and not re.search(r"(?i)\b(?:piano|violin|viola|cello|soprano|mezzo|tenor|bass|baritone|conductor|"
                                r"director|direction|guitar|harpsichord|organ|flute|oboe|clarinet|voice|counter"
                                r"tenor|lute|horn|trumpet|et al|op|no|nos)\b|\d", m.group(2)):
            i = 0 if texts[0].endswith(m.group(0)[-len(m.group(2)) - 2:]) and m.string == texts[0] else len(texts) - 1
            texts[i], label = m.group(1).strip(), m.group(2).strip()
    comps: list[str] = []
    work = None
    perf: list[str] = []
    for i, t in enumerate(texts):
        cw = composer_split(t)
        if not cw:
            continue
        comps = [c for c in _COMP_SEP.split(cw[0]) if c]
        nxt = composer_split(texts[i + 1]) if i + 1 < len(texts) else None
        if len(comps) > 1 and nxt and nxt[0] == comps[0]:           # 'Schumann & Mahler: Lieder' over the list
            continue
        parts = [cw[1].rstrip(";, ")]
        j = i + 1
        while j < len(texts) and composer_split(texts[j]):            # 'Franck: String Quartet in D'
            c2, w2 = composer_split(texts[j])
            parts.append(f"{c2}: {w2.rstrip(';, ')}")
            comps += [c for c in _COMP_SEP.split(c2) if c and c not in comps]
            j += 1
        work = "; ".join(parts)
        perf = texts[j:]
        if not perf:
            m = re.search(r"[a-zà-ÿ](?=[A-ZÀ-Þ][a-zà-ÿ])|\);\s*(?=[A-ZÀ-Þ])", work)     # 'Orchestral SongsSoile'
            if m and re.search(r"/|\(\w", work[m.end():]):
                work, perf = work[:m.start() + 1].rstrip("; "), [work[m.end():].lstrip("; ")]
            else:
                perf = [all_caps_name(x) for x in texts[:i]
                        if not {c for c in _COMP_SEP.split(x) if c} <= set(comps)][-1:]
        break
    else:
        lead = next((i for i, t in enumerate(texts) if name_like(t) and len(texts) - i >= 3), None)
        if lead is not None and (lead == 0 or styles[lead] == "head" or _COMP_SEP.search(texts[lead])):
            comps, work, perf = [c for c in _COMP_SEP.split(texts[lead]) if c], texts[lead + 1], texts[lead + 2:]
        elif texts:
            for i, t in enumerate(texts[:-1]):
                kp = known_prefix(t, composers)
                if kp:
                    (comps, work), perf = kp, texts[i + 1:]
                    break
    if not comps or not work or not perf:
        return None
    for k, c in enumerate(comps[:-1]):                                  # 'Felix & Fanny Mendelssohn'
        if len(c.split()) == 1 and len(comps[k + 1].split()) > 1 and c not in composers:
            comps[k] = f"{c} {comps[k + 1].split()[-1]}"
    if len(comps) > 1 and work.startswith(comps[0] + " "):            # 'Fagerlund & Aho' / 'Fagerlund Bassoon..'
        work = work[len(comps[0]) + 1:]
    while len(perf) > 1:                                                # 'Tchaikovsky Piano Concerto No. 1'
        own = next((c for c in comps[1:] if perf[0].startswith(c + " ")), None)
        kp = ([own], perf[0][len(own) + 1:]) if own else (
            known_prefix(perf[0], composers) if not re.search(r"[(/;,]", perf[0]) else None)
        if not kp or len(kp[0]) != 1:
            break
        work += f"; {kp[0][0]}: {kp[1]}"
        perf = perf[1:]
    # the artist taken for the composer ('Andrey Gugnin' / 'Shostakovich 24 Preludes'): retry from the work line
    surn = re.escape(surname(comps[0]))
    if len(comps) == 1 and any(re.search(rf"(?<![\w-]){surn}(?![\w-])", fold_text(p).casefold()) and not is_ens(p)
                               for p in re.split(r"\s*[;,/]\s*", "; ".join(perf))):
        kp = known_prefix(work, composers)
        if kp and kp[1]:
            comps, work = kp
        elif not re.match(r"(?i)^(?:works|music|songs|pieces) by\b", texts[texts.index(work) + 1] if work in texts
                          and texts.index(work) + 1 < len(texts) else ""):
            return None
    return {"composers": comps, "work": work, "performers": "; ".join(perf), "label": label}


_ABBR = [(r"\bLSO\b", "London Symphony Orchestra"), (r"\bLPO\b", "London Philharmonic Orchestra"),
         (r"\bRPO\b", "Royal Philharmonic Orchestra"), (r"\bCBSO\b", "City of Birmingham Symphony Orchestra"),
         (r"\bOAE\b", "Orchestra of the Age of Enlightenment"), (r"\bASMF\b", "Academy of St Martin in the Fields"),
         (r"\bSCO\b", "Scottish Chamber Orchestra"), (r"\bCOE\b", "Chamber Orchestra of Europe"),
         (r"\bSO\b", "Symphony Orchestra"), (r"\bPO\b", "Philharmonic Orchestra"), (r"\bCO\b", "Chamber Orchestra"),
         (r"\bOrch\b\.?", "Orchestra"), (r"\bPhil\b\.?(?= (?:Choir|Chorus|Orchestra))", "Philharmonic"),
         (r"\bEns\b\.?", "Ensemble"), (r"\bSi[rt]\s+(?=[A-Z])|\bDame\s+", "")]
_ROLE = re.compile(r"\s*\([^()]*\)")


def bbc_people(text: str) -> list[str]:
    """'Johan Botha, Petra Lang; WDR SO, Cologne/Semyon Bychkov' -> conductor, soloists, then ensembles
    (the order of the other lists: the conductor or leader first when there is one)."""
    t = re.sub(r"(?i)\s*\b(?:et al|etc|and others)\b\.?", "", _ROLE.sub("", text))
    t = re.sub(r"(?i)\b(?:dir(?:ector)?\b\.?|directed by|stage director)\s*:?\s*[^;,/]+", "", t)
    for a, b in _ABBR:
        t = re.sub(a, b, t)
    people: list[str] = []
    conductors: list[str] = []
    groups: list[str] = []
    t = re.sub(r"(?<!\s)[*^†]+|[*^†]+(?!\s)", "", t)
    # 'Raphaë l Pichon', 'Jó zef Orliń ski': a space after an accented letter, inside a word
    t = re.sub(r"(?<=[à-ž]) (?=[a-z]+\b)(?!(?:de|van|von|da|di|del|der|la|le|du|y|und|et|al)\b)", "", t)
    for chunk in re.split(r"\s*;\s*", t):
        chunk = _ROLE.sub("", chunk)
        m = re.match(r"^(.*?\b(?:Trio|Quartet|Quintet|Ensemble|Consort))\s+[-–]\s+.+$", chunk)
        if m:                                       # 'Smetana Trio - Jitka Čechová (piano), ...': the group only
            chunk = m.group(1)
        if "/" in chunk:
            left, _, right = chunk.rpartition("/")
            conductors += [c for c in re.split(r"\s*(?:,|&|\band\b)\s*", _ROLE.sub("", right)) if c.strip()]
            chunk = left
        pieces: list[str] = []
        for piece in re.split(r"\s*,\s*", chunk):
            bare = _ROLE.sub("", piece).strip(" .-–")
            if not bare:
                continue
            if pieces and (len(bare.split()) == 1 and not is_ens(bare)):  # 'WDR Symphony Orchestra, Cologne'
                pieces[-1] += ", " + bare
            elif " & " in bare and not is_ens(bare):                     # 'Jean-Guihen Queyras & Alexander Melnikov'
                pieces += bare.split(" & ")
            else:
                pieces.append(bare)
        for piece in pieces:
            (groups if is_ens(piece) else people).append(piece.strip())
    conductors = [c.strip(" .") for c in conductors if c.strip(" .")]
    out = conductors + people + groups if len(people) > 2 or not people else people + conductors + groups
    return list(dict.fromkeys(p for p in out if p))


def is_ens(name: str) -> bool:
    return bool(re.search(r"(?i)\b(?:orchest\w*|philharmon\w*|symphon\w*|sinfonia|sinfonietta|choir|chorus|"
                          r"consort|ensemble|quartet\w*|quatuor|quintet|trio|players|singers|band|kapelle|"
                          r"staatskapelle|camerata|academy|concert|baroque|soloists|voices|collegium|"
                          r"capella|cappella|chapel|vocal|opera|company|les |il |la |the )", name)
                or re.search(r"(?i)^(?:tenebrae|pygmalion|chineke!?|phantasm|fretwork|stile antico|"
                             r"i fagiolini|the sixteen|the tallis scholars|arcangelo|vox luminis)$", name))


# where a page's list of winners starts, when prose (naming the categories too) comes first
BBC_START = {2014: r"full list of winners", 2015: r"winners in full", 2024: r"award winners 2024$",
             2025: r"awards 2025 - the winners$", 2026: r"award winners 2026$"}


def page_lines(cache: pathlib.Path, year: int, kind: str, cat: str | None) -> list[tuple[str, str]]:
    lines = html_lines(decode((cache / page_file(year, kind, cat)).read_bytes()))
    if year in BBC_START and kind == "list":
        i = max(i for i, (t, _) in enumerate(lines) if re.search(BBC_START[year], t, re.IGNORECASE))
        lines = lines[i + 1:]
    if kind == "one":                               # one winner a page: the heading (kept) to the review
        i = max(i for i, (t, st) in enumerate(lines)
                if st == "head" and re.match(r"(?i)awards \d{4} -", t) or "best-selling classical" in t)
        j = next((j for j in range(i + 1, len(lines)) if re.match(r"(?i)excerpt|buy this disc|what the jury",
                                                                   lines[j][0])), len(lines))
        lines = lines[i:j]
        out: list[tuple[str, str]] = []
        for t, st in lines:                         # lines broken inside a work or performer list
            if out and len(out) > 1 and (out[-1][0].endswith((";", ",")) or t[0] in "(" or t[0].islower()):
                out[-1] = (f"{out[-1][0]} {t}", out[-1][1])
            else:
                out.append((t, st))
        lines = out
    return lines


def disc_blocks(lines: list[tuple[str, str]]) -> list[tuple[list[tuple[str, str]], str]]:
    """All the discs under a nominees heading, each ending at its label line."""
    out, cur = [], []
    for text, style in lines:
        if _NOISE.match(text) or len(text) > 300:
            continue
        if is_label_line(text, style):
            if cur:
                out.append((cur, label_text(text)))
            cur = []
        else:
            cur.append((text, style))
    return out


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", fold_text(s).lower())} - {
        "with", "from", "works", "orchestra", "symphony", "philharmonic", "award", "their", "this", "that",
        "piano", "violin", "cello", "concerto", "concertos", "music", "choir", "chorus", "ensemble", "quartet"}


def announced_winner(prose: str, cat: str, discs: list[dict]) -> dict | None:
    """The nominee the winners announcement names for a category: the one disc whose composer, work and
    performer words are most often in the passage about that category (from the previous category named to
    the next), and clearly ahead of the others."""
    names = {"Recording of the Year": r"recording of the year|most prestigious"}
    pat = re.compile(r"(?i)\b(?:" + names.get(cat, re.escape(cat) + r" (?:award|prize)") + r")")
    any_cat = re.compile(r"(?i)\b(?:(?:orchestral|concerto|opera|choral|chamber|vocal|instrumental|premiere|"
                         r"newcomer) (?:award|prize)|recording of the year|personality of the year)")
    best: list[tuple[int, int]] = []
    for para in prose.split("\n"):
        for m in pat.finditer(para):
            starts = [x.end() for x in any_cat.finditer(para, 0, m.start())]
            ends = [x.start() for x in any_cat.finditer(para, m.end())]
            passage = para[starts[-1] if starts else 0:ends[0] if ends else len(para)]
            words = _tokens(passage)
            for i, d in enumerate(discs):
                score = len(words & _tokens(" ".join([*d["composers"], d["work"], d["performers"]])))
                best.append((score, i))
    if not best:
        return None
    top: dict[int, int] = collections.defaultdict(int)
    for score, i in best:
        top[i] = max(top[i], score)
    ranked = sorted(top.items(), key=lambda x: -x[1])
    if ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] >= ranked[1][1] + 2):
        return discs[ranked[0][0]]
    return None


def is_compilation(disc: dict, surnames: set[str]) -> bool:
    """A recital or anthology ('Works by Fauré, Ravel...', an aria disc credited to its singer's lead composer):
    no one composer's work to match. `surnames`: the known composers' folded surnames."""
    work = disc["work"]
    comp, others = disc["composers"][0], disc["composers"][1:]
    named = set(re.findall(r"[\w'-]+", fold_text(work).casefold())) & surnames
    return bool(re.match(r"(?i)^(?:works|music|songs|pieces|arias)\b.*\bby\b", work)
                or re.search(r"(?i)\bworks by\b", work + " " + disc["performers"])
                or (surname(comp) not in surnames and not others and (len(named) >= 2 or re.search(
                    r"(?i)\b(?:arias|songs|mélodies|melodies|works|pieces)\b|\((?:[a-z -]+)\)", work))))


def parse_bbc(cache: pathlib.Path, known: frozenset[str], drops: collections.Counter) -> list[dict]:
    """The BBC Music Magazine Award winners, one row per category (a disc that won two categories is two
    rows). `drops` counts the winners that could not be read."""
    out: list[dict] = []
    base_surnames = {surname(p) for c in known for p in _COMP_SEP.split(c) if p}
    surnames = base_surnames

    def add(year: int, cat: str, disc: dict | None, url: str) -> None:
        if disc is None:
            drops["unreadable"] += 1
            return
        work = disc["work"]
        comp, others = disc["composers"][0], disc["composers"][1:]
        if is_compilation(disc, surnames):
            drops["compilation"] += 1                 # a recital: no one composer's work to find
            return
        if others and ":" not in work:              # 'Sibelius • Nielsen: Violin Concertos'
            work = f"{work}; {', '.join(others)}: {work}"
        out.append({"award": BBC, "category": cat, "year": year, "result": "winner", "composer": comp,
                    "work": work, "performers": bbc_people(disc["performers"]), "label": disc["label"],
                    "url": url})

    by_year: dict[int, list] = collections.defaultdict(list)
    for page in BBC_PAGES:
        by_year[page[0]].append(page)
    for year, pages in sorted(by_year.items()):
        prose = " ".join(t for _, k, c, _, _ in pages
                         for t, _ in html_lines(decode((cache / page_file(year, k, c)).read_bytes())))
        composers = known | {m for m in re.findall(r"((?:[A-ZÀ-Þ][\w-]+ ){0,2}[A-ZÀ-Þ][\w-]+)[’']s\b", prose)
                             if not re.search(r"BBC|Music|Magazine|Award|Year|The ", m)}
        surnames = base_surnames | {surname(c) for c in composers}
        kinds = {k for _, k, _, _, _ in pages}
        if kinds == {"one"}:
            for _, _, cat, url, wb in pages:
                lines = page_lines(cache, year, "one", cat)
                cats = bbc_category(lines[0][0]) or []
                block, label = winner_block(lines[1:])
                disc = bbc_disc(block, label, None, composers)
                for c in dict.fromkeys([cat, *cats]):
                    add(year, c, disc, page_url(url, wb))
        elif kinds == {"list"}:
            _, _, _, url, wb = pages[0]
            for cats, artist, sec in bbc_sections(page_lines(cache, year, "list", None)):
                if not cats:
                    continue
                block, label = winner_block(sec)
                disc = bbc_disc(block, label, artist, composers)
                for c in cats:
                    add(year, c, disc, page_url(url, wb))
        else:                                       # nominees, and the winners named in prose
            nom = next(p for p in pages if p[1] == "nominees")
            ann = next(p for p in pages if p[1] == "announce")
            prose = "\n".join(t for t, _ in page_lines(cache, year, "announce", None))
            everything: list[dict] = []
            for cats, artist, sec in bbc_sections(page_lines(cache, year, "nominees", None)):
                if len(cats) != 1:
                    continue
                discs = [d for d in (bbc_disc(b, lab, None, composers) for b, lab in disc_blocks(sec)) if d]
                everything += discs
                win = announced_winner(prose, cats[0], discs)
                add(year, cats[0], win, page_url(ann[3], ann[4]))
            win = announced_winner(prose, "Recording of the Year", everything)
            add(year, "Recording of the Year", win, page_url(ann[3], ann[4]))
            del nom
    return out


# ---- the dataset -----------------------------------------------------------------------------------------------


def clean(p: str) -> str:
    return re.sub(r"\(\s*\)", "", p).strip(" .,;-–")


def entry(r: dict) -> dict | None:
    perf = [p for p in dict.fromkeys(clean(p) for p in r["performers"]) if p and len(p) > 2 and not re.search(r"\d", p)]
    if not r.get("composer") or not r.get("work") or not perf:
        return None
    comp, work = clean(r["composer"]), clean(r["work"].replace("''", ""))
    # wikitext the parsers could not take apart (a nominee list run into a row, a template's end): drop the row
    if any(re.search(r"''|[{}*|]|==", x) for x in [r["composer"], *perf]):
        return None
    if len(comp.split()) > 4 or re.search(r"\d|complete|works|vol\b", comp, re.IGNORECASE) or len(work) < 3:
        return None
    e = {"aw": r["award"], "cat": r.get("category"), "y": r["year"], "res": r["result"], "comp": comp, "work": work,
         "perf": perf, "src": SRC[r["award"]], "url": r.get("url"),
         "hist": bool(r.get("category") and re.search(r"histor|archive|remaster", r["category"], re.IGNORECASE))}
    if r.get("label"):
        e["label"] = clean(r["label"])
    key = json.dumps([e["aw"], e["cat"], e["y"], e["res"], comp, work, perf], ensure_ascii=False)
    e["id"] = hashlib.sha1(key.encode()).hexdigest()[:10]
    return e


def category_url(award: str, category: str, source: int) -> str:
    if award == "Grammy":
        page = next(p for p, c in GRAMMY_PAGES.items() if c == category)
        return WP + ARTICLE.get(page, page)
    return SOURCES[source]["url"]


def build(wiki: pathlib.Path, drops: collections.Counter | None = None) -> tuple[str, list[dict]]:
    raw = parse_grammy(wiki) + [r for r in parse_gramophone(wiki) if not SKIP.search(r["category"] or "")]
    out = [e for e in map(entry, raw + parse_diapason(wiki)) if e]
    drops = collections.Counter() if drops is None else drops
    bbc = parse_bbc(wiki, frozenset(e["comp"] for e in out), drops)
    new = [e for e in map(entry, bbc) if e]
    drops["filtered"] += len(bbc) - len(new)
    # newer Grammy lists name the winner among the nominees too: keep it once, as the winner
    wp = {(e["cat"], e["y"], e["comp"], e["work"]): set(e["perf"]) for e in out
          if e["aw"] == "Grammy" and e["res"] == "winner"}
    out = [e for e in out if not (e["aw"] == "Grammy" and e["res"] == "nominee"
                                  and (e["cat"], e["y"], e["comp"], e["work"]) in wp
                                  and (set(e["perf"]) & wp[(e["cat"], e["y"], e["comp"], e["work"])]
                                       or not e["perf"]))]
    # categories added after the first lists go after theirs, so the older rows keep their category numbers
    cats = sorted({(e["aw"], e["cat"] or "", e["src"], e["hist"]) for e in out})
    cats += sorted({(e["aw"], e["cat"] or "", e["src"], e["hist"]) for e in new} - set(cats))
    out += new
    ids = [e["id"] for e in out]
    assert len(ids) == len(set(ids)), "duplicate entry ids"
    ci = {c[:2]: i for i, c in enumerate(cats)}
    rows = [[e["id"], ci[(e["aw"], e["cat"] or "")], e["y"], e["res"], e["comp"], e["work"], e["perf"]]
            + ([e.get("label"), e["url"]] if e.get("url") else [e["label"]] if e.get("label") else []) for e in out]
    head = {"version": 1, "compiled": COMPILED, "about": ABOUT,
            "columns": ["id", "category", "year", "result", "composer", "work", "performers", "label", "url"],
            "sources": SOURCES,
            "categories": [{"award": a, "category": c or None, "source": s, **({"historical": True} if h else {}),
                            "url": category_url(a, c, s)} for a, c, s, h in cats]}
    text = (json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "entries":[\n'
            + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n ]\n}\n")
    json.loads(text)
    return text, out


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--fetch":
        argv = argv[1:]
        fetch(pathlib.Path(argv[0]))
    if not argv:
        sys.exit(__doc__)
    out = pathlib.Path(argv[1] if len(argv) > 1 else
                       pathlib.Path(__file__).resolve().parent.parent / "classical_core/data/accolades.json")
    drops: collections.Counter = collections.Counter()
    text, entries = build(pathlib.Path(argv[0]), drops)
    out.write_text(text)
    print(len(entries), "entries,", len(text.encode()), "bytes:",
          dict(collections.Counter(f"{e['aw']} {e['res']}" for e in entries)), "BBC winners dropped:", dict(drops))


if __name__ == "__main__":
    main(sys.argv[1:])
