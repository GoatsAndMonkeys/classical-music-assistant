"""Liner notes for the work and performer pages, MA-free.

* What a work's notes are about (`refs`): the work page's own facts (dates, key, catalogue), the movements of
  one recording as titled in the library, and the MusicBrainz ids of the work, its composers and that
  recording's conductor, orchestra and first soloists. A performer's id comes only from a MusicBrainz
  recording credit on one of the recording's own tracks whose name is one of the performer's names, exact.
* The Wikipedia text itself is fetched by the glue through curated links only (MusicBrainz url-rels → Wikidata
  → enwiki sitelink), and cached here in `liner_note` for `NOTES_TTL` (a null value records "there is none").
* The optional listening guide is written by the configured language model from those facts alone
  (`guide_prompt`), at most once per work: a refusal or an unusable reply is kept as failed for good, an HTTP or
  network error is retried no sooner than `GUIDE_RETRY` later, and only when the work page is opened again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import views
from .index import Index
from .names import LLMRefused
from .text import ident_key

NOTES_TTL = 90 * 86400.0
GUIDE_RETRY = 86400.0
GUIDE_MAX_TOKENS = 4000
GUIDE_MAX_CHARS = 2400
MAX_CONDUCTORS = 2
MAX_ORCHESTRAS = 1
MAX_SOLOISTS = 3
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---- what the notes are about --------------------------------------------------------------------------------

def _mbids(ix: Index, entity: str, eid: str) -> list[str]:
    return [m for r in ix._q("SELECT value FROM alias WHERE entity=? AND kind='mbid' AND entity_id=? ORDER BY value",
                             (entity, eid)) if _MBID.match(m := (r[0] or "").lower())]


def _credit_mbids(ix: Index, track_ids: list[str]) -> dict[str, str]:
    """ident_key(credited name) → MusicBrainz artist id, from the MusicBrainz recordings of these tracks. A
    name credited to more than one MusicBrainz artist (two namesakes) is left out: no bio beats a wrong one."""
    if not track_ids:
        return {}
    marks = ",".join("?" * len(track_ids))
    recs = {r[0].lower() for r in ix._q(f"SELECT mbid FROM mapping_state WHERE library_track_id IN ({marks}) "
                                        "AND mbid IS NOT NULL", track_ids)}
    for r in ix._q(f"SELECT value FROM evidence WHERE entity='release_track' AND field='recording_mbid' "
                   f"AND entity_id IN ({marks}) AND retracted_at IS NULL", track_ids):   # a strict release match
        try:
            v = json.loads(r[0])
        except ValueError:
            continue
        if isinstance(v, str):
            recs.add(v.lower())
    out: dict[str, str] = {}
    clash: set[str] = set()
    for rec in recs:
        for r in ix._q("SELECT value FROM evidence WHERE entity='mb_recording' AND entity_id=? AND field='credit' "
                       "AND retracted_at IS NULL", (rec,)):
            try:
                c = json.loads(r[0])
            except ValueError:
                continue
            mbid = (c.get("mbid") or "").lower() if isinstance(c, dict) else ""
            if (_MBID.match(mbid) and (k := ident_key(c.get("name") or ""))
                    and out.setdefault(k, mbid) != mbid):
                clash.add(k)
    return {k: v for k, v in out.items() if k not in clash}


def _person_keys(ix: Index, pid: str, name: str) -> set[str]:
    keys = {r[0] for r in ix._q("SELECT value FROM alias WHERE entity='person' AND kind='name' AND entity_id=?",
                                (pid,))}
    return keys | ({k} if (k := ident_key(name)) else set())


def _performers(credits: list[dict]) -> list[dict]:
    """The recording's principal performers: conductors, then an orchestra, then the first soloists."""
    caps = {"conductors": MAX_CONDUCTORS, "orchestras": MAX_ORCHESTRAS, "soloists": MAX_SOLOISTS}
    out: list[dict] = []
    for group, cap in caps.items():
        seen = [c for c in credits if c.get("group") == group and not any(o["id"] == c["id"] for o in out)]
        out += list({c["id"]: c for c in seen}.values())[:cap]
    return out


def refs(ix: Index, wid: str, rid: str | None = None, vis=None) -> dict[str, Any]:
    """The facts and MusicBrainz ids the notes need; NotFound for an unknown or hidden work. `rid` picks the
    recording whose movements and performers are shown (a recording of another work is ignored), else the
    work page's pick."""
    w = views.work(ix, wid, vis=vis)
    work = w["work"]
    rec = None
    for cand in (rid, (w.get("best") or {}).get("recording_id"),
                 w["recordings"][0]["id"] if w["recordings"] else None):
        if not cand:
            continue
        try:
            r = views.recording(ix, cand, vis)
        except views.NotFound:
            continue
        if r["work"]["id"] == work["id"]:
            rec = r
            break
    movements = [m["name"] for m in (rec or {}).get("movements") or [] if (m.get("name") or "").strip()]
    by_key = _credit_mbids(ix, [m["track_id"] for m in (rec or {}).get("movements") or []])
    performers = []
    for c in _performers((rec or {}).get("credits") or []):
        mbid = next((by_key[k] for k in sorted(_person_keys(ix, c["id"], c["name"])) if k in by_key), None)
        performers.append({"id": c["id"], "name": c["name"], "role": c.get("role"), "group": c.get("group"),
                           "mbid": mbid})
    wm = _mbids(ix, "work", work["id"])
    return {
        "work": work, "work_mbid": wm[0] if wm else None, "recording_id": rec["id"] if rec else None,
        "movements": movements,
        "composers": [{"id": c["id"], "name": c["name"], "role": "composer", "group": "composers",
                       "mbid": next(iter(_mbids(ix, "composer", c["id"])), None)} for c in work.get("composers") or []],
        "performers": performers,
    }


def performer_mbid(ix: Index, pid: str, vis=None) -> tuple[str, str | None]:
    """(name, MusicBrainz id) of a performer, the id from a credit on any of their tracks (see `refs`);
    NotFound for an unknown or hidden performer."""
    me = views.performer(ix, pid, vis)["performer"]
    tracks = [r[0] for r in ix._q("SELECT DISTINCT o.library_track_id FROM track_credit c JOIN occurrence o "
                                  "ON o.id=c.occurrence_id WHERE c.person_id=? LIMIT 400", (me["id"],))]
    by_key = _credit_mbids(ix, tracks)
    return me["name"], next((by_key[k] for k in sorted(_person_keys(ix, me["id"], me["name"])) if k in by_key), None)


# ---- the Wikipedia cache -------------------------------------------------------------------------------------

def cached(con: sqlite3.Connection, key: str, now: float, ttl: float = NOTES_TTL) -> tuple[bool, Any]:
    """(fresh, value): fresh is False when the key was never fetched or is older than `ttl`; a stale value is
    still returned, to show while a refresh fails."""
    row = con.execute("SELECT data_json, fetched_at FROM liner_note WHERE key=?", (key,)).fetchone()
    if row is None:
        return False, None
    try:
        value = json.loads(row[0]) if row[0] is not None else None
    except ValueError:
        return False, None
    return now - row[1] <= ttl, value


def store(con: sqlite3.Connection, key: str, value: Any, now: float) -> None:
    con.execute("INSERT OR REPLACE INTO liner_note(key, data_json, fetched_at) VALUES (?,?,?)",
                (key, json.dumps(value, ensure_ascii=False) if value is not None else None, now))


# ---- the listening guide -------------------------------------------------------------------------------------

GUIDE_SYSTEM = (
    "You write short listening guides for classical works, for a music library's liner notes. Use only the "
    "facts in the user's message: they are everything you may rely on. Do not add dates, names, numbers, "
    "anecdotes, quotations or history that the facts do not state, and do not guess. Write about 150 to 250 "
    "words in plain prose, no headings, lists or markdown: one or two sentences of orientation, then one or "
    "two sentences per movement (in the order given, named as given) on what a listener can listen for. When "
    "the facts say little about a movement, describe only what its title or tempo marking tells you. If the "
    "facts are not enough to say anything useful, reply with exactly: NO GUIDE")


def guide_facts(ref: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    """Everything the guide may use, and nothing else: the work's own facts, its Wikipedia summary and the
    movement titles. Performers are left out: the guide is about the work, not one recording."""
    work = ref["work"]
    dates = work.get("dates") or {}
    prem = dates.get("premiered") or {}
    facts: dict[str, Any] = {
        "title": work.get("title"),
        "composers": [c["name"] for c in work.get("composers") or []],
        "key": work.get("key"), "catalogue": work.get("catalogue"),
        "composed": (dates.get("composed") or {}).get("text"),
        "premiered": " ".join(x for x in (prem.get("text"), f"({prem['place']})" if prem.get("place") else None)
                              if x) or None,
        "summary": (summary or {}).get("text"),
        "movements": list(ref.get("movements") or []),
    }
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def guide_prompt(facts: dict[str, Any]) -> tuple[str, str]:
    """(system, user): the user message is the facts as JSON and nothing more."""
    return GUIDE_SYSTEM, json.dumps(facts, ensure_ascii=False, indent=1)


def clean_guide(text: str) -> str:
    """The guide as plain paragraphs; LLMRefused when the model declined or wrote nothing usable."""
    text = re.sub(r"(?m)^\s*#+\s.*$", "", text or "")              # a stray heading
    text = re.sub(r"\*\*|__|(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", text)  # bold and italic markers
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    text = "\n\n".join(p for p in paras if p)
    if not text or text.strip(" .").upper() == "NO GUIDE" or len(text.split()) < 40:
        raise LLMRefused("no usable guide")
    if len(text) > GUIDE_MAX_CHARS:
        cut = text[:GUIDE_MAX_CHARS]
        text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut.rstrip() + "…"
    return text


async def write_guide(llm: Any, facts: dict[str, Any]) -> str:
    """One model call; the cleaned guide. Raises on any failure (LLMRefused: do not ask again)."""
    system, user = guide_prompt(facts)
    return clean_guide(await llm.complete(system, user, max_tokens=GUIDE_MAX_TOKENS))


def guide_get(con: sqlite3.Connection, work_mbid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT text, model, status, error, created_at FROM listening_guide WHERE work_mbid=?",
                      (work_mbid,)).fetchone()
    return {"text": row[0], "model": row[1], "status": row[2], "error": row[3], "created_at": row[4]} \
        if row else None


def guide_due(row: dict[str, Any] | None, now: float) -> bool:
    """Whether to ask the model: never asked, or only a transient error at least GUIDE_RETRY ago."""
    return row is None or (row["status"] == "error" and now - row["created_at"] >= GUIDE_RETRY)


def guide_put(con: sqlite3.Connection, work_mbid: str, model: str, now: float, text: str | None = None,
              status: str = "ok", error: str | None = None) -> None:
    con.execute("INSERT OR REPLACE INTO listening_guide(work_mbid, text, model, status, error, created_at) "
                "VALUES (?,?,?,?,?,?)", (work_mbid, text, model, status, error, now))
