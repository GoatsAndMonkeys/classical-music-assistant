"""Rebuild classical_core/data/accolades.json from Wikipedia wikitext (docs/accolades.md).

    python scripts/build_accolades.py WIKI_DIR [OUT]          # parse saved pages (WIKI_DIR/<Page>.wiki)
    python scripts/build_accolades.py --fetch WIKI_DIR [OUT]  # first download the pages' wikitext (action=raw)

Facts only: award, category, year, result, composer, work, performers (and a label when the list names one).
No prose is copied. Entry ids are sha1 hashes of those facts, so a rebuild keeps the ids user decisions refer to.
The parsers are deliberately simple; rows they cannot read cleanly are dropped, never guessed.
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import re
import sys
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
]
SRC = {"Grammy": 0, "Gramophone Award": 1, "Diapason d'Or de l'Année": 2}
# Gramophone categories that are not a recording of a work
SKIP = re.compile(r"artist|lifetime|label|achievement|orchestra of the year|engineering|best-selling|young|concept|"
                  r"video|dvd|music theatre|special|piano$", re.IGNORECASE)
ABOUT = ("Classical award winners and Grammy nominations, compiled from the public lists named in `sources` "
         "(facts only: award, year, composer, work, performers; no review text). Grammy years are ceremony years "
         "(the release is one to two years earlier); Gramophone and Diapason years are award years. Rebuilt by the "
         "parser described in docs/accolades.md; entry ids are stable hashes, so user decisions survive a rebuild.")


def file_name(page: str) -> str:
    return page.replace("'", "_27") + ".wiki"


def fetch(wiki: pathlib.Path) -> None:
    wiki.mkdir(parents=True, exist_ok=True)
    for page in [*GRAMMY_PAGES, GRAMOPHONE, DIAPASON]:
        title = ARTICLE.get(page, page)
        url = "https://en.wikipedia.org/w/index.php?" + urllib.parse.urlencode({"title": title, "action": "raw"})
        req = urllib.request.Request(url, headers={"User-Agent": "classical-music-assistant dataset build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            (wiki / file_name(page)).write_bytes(r.read())


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
         "perf": perf, "src": SRC[r["award"]],
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


def build(wiki: pathlib.Path) -> tuple[str, list[dict]]:
    raw = parse_grammy(wiki) + [r for r in parse_gramophone(wiki) if not SKIP.search(r["category"] or "")]
    out = [e for e in map(entry, raw + parse_diapason(wiki)) if e]
    # newer Grammy lists name the winner among the nominees too: keep it once, as the winner
    wp = {(e["cat"], e["y"], e["comp"], e["work"]): set(e["perf"]) for e in out
          if e["aw"] == "Grammy" and e["res"] == "winner"}
    out = [e for e in out if not (e["aw"] == "Grammy" and e["res"] == "nominee"
                                  and (e["cat"], e["y"], e["comp"], e["work"]) in wp
                                  and (set(e["perf"]) & wp[(e["cat"], e["y"], e["comp"], e["work"])]
                                       or not e["perf"]))]
    ids = [e["id"] for e in out]
    assert len(ids) == len(set(ids)), "duplicate entry ids"
    cats = sorted({(e["aw"], e["cat"] or "", e["src"], e["hist"]) for e in out})
    ci = {c[:2]: i for i, c in enumerate(cats)}
    rows = [[e["id"], ci[(e["aw"], e["cat"] or "")], e["y"], e["res"], e["comp"], e["work"], e["perf"]]
            + ([e["label"]] if e.get("label") else []) for e in out]
    head = {"version": 1, "compiled": COMPILED, "about": ABOUT,
            "columns": ["id", "category", "year", "result", "composer", "work", "performers", "label"],
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
    text, entries = build(pathlib.Path(argv[0]))
    out.write_text(text)
    print(len(entries), "entries,", len(text.encode()), "bytes:",
          dict(collections.Counter(f"{e['aw']} {e['res']}" for e in entries)))


if __name__ == "__main__":
    main(sys.argv[1:])
