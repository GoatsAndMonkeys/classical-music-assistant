#!/usr/bin/env python3
"""Compare two index snapshots (scripts/snapshot.py) and fail on regressions (docs/deploy.md).

    python scripts/compare_snapshots.py before.json after.json [--json] [--warn-only CAT,...]

Exit status: 0 = no regressions (warnings allowed), 1 = regressions, 2 = bad input.

Albums only in `after` are growth from the library import and are summarised, not judged. Everything else
is judged on the albums both snapshots know ("existing" albums: indexed before and still in the index after):
  composer_merge     two or more composers' tracks now credited to one composer (the Medtner collapse)
  composer_gone      a composer with tracks on existing albums has none of them any more
  composer_changed   tracks of an existing album now credited to a different composer
  recordings_lost    tracks of an existing album that were in a recording and are in none now
  works_lost         a composer's work on an existing album that no longer exists under that composer
  accolades_lost     an accolade placement on an existing album that is gone (or now only "uncertain")
  accolades_moved    an accolade that now points at a different recording or work
  joins_removed      a cross-disc join between existing albums that was undone
  joins_added        a new cross-disc join between existing albums
Warnings (never fail unless --strict): works_retitled, recordings_regrouped, review_jump, albums_removed,
albums_unindexed, accolade_grade, non_latin_composer, count_drop.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

REGRESSIONS = ("composer_merge", "composer_gone", "composer_changed", "recordings_lost", "works_lost",
               "accolades_lost", "accolades_moved", "joins_removed", "joins_added")


def _album(key: str) -> str:
    return key.split(":", 1)[0]


def _has_latin(name: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in name)


def _acc_id(a: dict) -> tuple:
    return tuple(a.get(k) for k in ("album", "source", "source_ref", "kind", "award", "category", "year",
                                    "publication", "reviewer", "target"))


def compare(before: dict, after: dict, review_pct: float = 25.0, review_min: int = 10, limit: int = 25) -> dict:
    b_alb, a_alb = before["albums"], after["albums"]
    existing = {k for k, v in b_alb.items() if v.get("indexed") and k in a_alb and a_alb[k].get("indexed")}
    new_albums = sorted(set(a_alb) - set(b_alb), key=lambda k: (len(k), k))
    removed = sorted(set(b_alb) - set(a_alb))
    unindexed = sorted(k for k, v in b_alb.items() if v.get("indexed") and k in a_alb and not a_alb[k].get("indexed"))
    bt, at = before["tracks"], after["tracks"]
    findings: dict[str, list] = defaultdict(list)

    # --- per track: composer and coverage on existing albums
    moves: Counter = Counter()           # (before composer, after composer) -> tracks
    changed_albums: dict[str, set] = defaultdict(set)
    lost_tracks: dict[str, list] = defaultdict(list)
    regrouped: set = set()
    for tk, tb in bt.items():
        if _album(tk) not in existing:
            continue
        ta = at.get(tk)
        if ta is None:
            lost_tracks[tb["rec"]].append(tk)
            continue
        if ta["rec"] != tb["rec"]:
            regrouped.add((tb["rec"], ta["rec"]))
        if ta["composers"] != tb["composers"]:
            changed_albums[_album(tk)].add((" / ".join(tb["composers"]), " / ".join(ta["composers"]),
                                            tb["work"], ta["work"]))
            gone = set(tb["composers"]) - set(ta["composers"])
            got = set(ta["composers"]) - set(tb["composers"])
            for g in gone:
                for n in got or {"(none)"}:
                    moves[(g, n)] += 1

    receivers: dict[str, dict] = defaultdict(dict)
    for (src, dst), n in moves.items():
        receivers[dst][src] = n
    for dst, srcs in sorted(receivers.items()):
        if len(srcs) >= 2:
            findings["composer_merge"].append({"into": dst, "from": dict(sorted(srcs.items())),
                                               "tracks": sum(srcs.values())})
    for alb, ch in sorted(changed_albums.items(), key=lambda kv: (len(kv[0]), kv[0])):
        findings["composer_changed"].append({
            "album": alb, "name": b_alb[alb].get("name"),
            "changes": [{"from": f, "to": t, "work": w if w == w2 else f"{w} -> {w2}"} for f, t, w, w2 in sorted(ch)]})

    # composers whose tracks on existing albums all went away
    def comp_tracks(snap, albums):
        out: dict[str, int] = Counter()
        for tk, t in snap["tracks"].items():
            if _album(tk) in albums:
                for c in t["composers"]:
                    out[c] += 1
        return out
    cb, ca = comp_tracks(before, existing), comp_tracks(after, existing)
    for c, n in sorted(cb.items()):
        if not ca.get(c):
            went = {d: k for (s, d), k in moves.items() if s == c}
            findings["composer_gone"].append({"composer": c, "tracks_before": n, "now_credited_to": went})

    for rec, tks in sorted(lost_tracks.items()):
        findings["recordings_lost"].append({"recording": rec, "album": b_alb[_album(rec)].get("name"),
                                            "work": bt[tks[0]]["work"], "composers": bt[tks[0]]["composers"],
                                            "tracks_lost": len(tks)})
    lost_recs = {r for r, _ in regrouped} - {r for _, r in regrouped}
    for old, new in sorted(regrouped):
        if old in lost_recs:
            findings["recordings_regrouped"].append({"from": old, "to": new})

    # --- works per composer on existing albums, by (album, title)
    def works(snap):
        out = set()
        for c, d in snap["composers"].items():
            for rk, title in d["works"].items():
                if _album(rk) in existing:
                    out.add((c, _album(rk), title))
        return out
    wb, wa = works(before), works(after)
    a_by_comp_album = defaultdict(set)
    for c, alb, t in wa:
        a_by_comp_album[(c, alb)].add(t)
    for c, alb, title in sorted(wb - wa):
        still = a_by_comp_album.get((c, alb))
        entry = {"composer": c, "album": alb, "name": b_alb[alb].get("name"), "work": title}
        if still and not changed_albums.get(alb) and not any(_album(r) == alb for r in lost_tracks):
            findings["works_retitled"].append({**entry, "now": sorted(still - {t for cc, aa, t in wb
                                                                                 if cc == c and aa == alb})})
        else:
            findings["works_lost"].append(entry)

    # --- accolades
    def acc_index(snap, albums):
        out: dict[tuple, list] = defaultdict(list)
        for a in snap.get("accolades", []):
            if a["album"] in albums:
                out[_acc_id(a)].append(a)
        return out
    ab, aa = acc_index(before, existing), acc_index(after, existing)
    for k, rows in sorted(ab.items(), key=lambda kv: json.dumps(kv[0], default=str)):
        shown_b = sorted(((r["recording"], r["work"]) for r in rows if r["grade"] != "uncertain"), key=str)
        rows_a = aa.get(k, [])
        shown_a = sorted(((r["recording"], r["work"]) for r in rows_a if r["grade"] != "uncertain"), key=str)
        label = {"album": k[0], "name": b_alb[k[0]].get("name"), "accolade": " | ".join(str(x) for x in k[3:8] if x)}
        if shown_b and not shown_a:
            findings["accolades_lost"].append({**label, "was": shown_b})
        elif shown_b and shown_a != shown_b:
            findings["accolades_moved"].append({**label, "was": shown_b, "now": shown_a})
        elif sorted(r["grade"] for r in rows) != sorted(r["grade"] for r in rows_a):
            findings["accolade_grade"].append({**label, "was": sorted(r["grade"] for r in rows),
                                               "now": sorted(r["grade"] for r in rows_a)})

    # --- joins between existing albums
    jb = {tuple(j) for j in before.get("joins", []) if _album(j[0]) in existing and _album(str(j[1])) in existing}
    ja = {tuple(j) for j in after.get("joins", []) if _album(j[0]) in existing and _album(str(j[1])) in existing}
    for j in sorted(jb - ja):
        findings["joins_removed"].append({"joined": j[0], "lead": j[1]})
    for j in sorted(ja - jb):
        findings["joins_added"].append({"joined": j[0], "lead": j[1]})

    # --- review counts
    rb, ra = before.get("review_by_reason", {}), after.get("review_by_reason", {})
    for reason in sorted(set(rb) | set(ra)):
        x, y = rb.get(reason, 0), ra.get(reason, 0)
        if abs(y - x) >= review_min and (x == 0 or abs(y - x) * 100.0 / x > review_pct):
            findings["review_jump"].append({"reason": reason, "before": x, "after": y})

    for alb in removed:
        findings["albums_removed"].append({"album": alb, "name": b_alb[alb].get("name")})
    for alb in unindexed:
        findings["albums_unindexed"].append({"album": alb, "name": b_alb[alb].get("name")})
    for c in sorted(set(after["composers"]) - set(before["composers"])):
        if not _has_latin(c):
            findings["non_latin_composer"].append({"composer": c})
    for k, x in before["counts"].items():
        y = after["counts"].get(k)
        if isinstance(x, int) and isinstance(y, int) and y < x and k not in ("review_items", "recordings_visible"):
            findings["count_drop"].append({"count": k, "before": x, "after": y})

    new_tracks = sum(1 for tk in at if _album(tk) not in b_alb)
    new_comps = sorted(set(comp_tracks(after, set(new_albums))) - set(before["composers"]))
    return {
        "existing_albums": len(existing),
        "growth": {"new_albums": len(new_albums), "new_album_tracks_in_recordings": new_tracks,
                   "new_composers": len(new_comps), "new_composer_names": new_comps[:limit]},
        "counts": {k: [before["counts"].get(k), after["counts"].get(k)] for k in after["counts"]},
        "findings": {k: v for k, v in findings.items() if v},
    }


def render(res: dict, regressions: set[str], limit: int) -> str:
    out = [f"existing albums compared: {res['existing_albums']}"]
    g = res["growth"]
    out.append(f"growth (library import): {g['new_albums']} new album(s), {g['new_album_tracks_in_recordings']} "
               f"track(s) in recordings, {g['new_composers']} new composer(s)")
    out.append("counts (before -> after):")
    for k, (x, y) in res["counts"].items():
        mark = "" if x == y else f"  ({'+' if (y or 0) >= (x or 0) else ''}{(y or 0) - (x or 0)})"
        out.append(f"  {k:24s} {x} -> {y}{mark}")
    for cat, items in res["findings"].items():
        sev = "REGRESSION" if cat in regressions else "warning"
        out.append(f"{sev}: {cat} ({len(items)})")
        for it in items[:limit]:
            out.append("  " + json.dumps(it, ensure_ascii=False, default=str))
        if len(items) > limit:
            out.append(f"  ... {len(items) - limit} more (use --json for all)")
    bad = [c for c in res["findings"] if c in regressions]
    out.append("RESULT: " + (f"REGRESSIONS in {', '.join(bad)}" if bad else "OK (no regressions)"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--warn-only", default="", help="comma-separated regression categories to report as warnings")
    ap.add_argument("--strict", action="store_true", help="treat every warning as a regression too")
    ap.add_argument("--review-pct", type=float, default=25.0, help="warn when a review reason moves by more (%%)")
    ap.add_argument("--review-min", type=int, default=10, help="...and by at least this many items")
    ap.add_argument("--limit", type=int, default=25, help="items printed per category")
    a = ap.parse_args(argv)
    try:
        with open(a.before, encoding="utf-8") as f:
            before = json.load(f)
        with open(a.after, encoding="utf-8") as f:
            after = json.load(f)
        for s in (before, after):
            if "tracks" not in s or "albums" not in s:
                raise ValueError("not a snapshot.py snapshot")
    except (OSError, ValueError) as e:
        print(f"compare_snapshots: {e}", file=sys.stderr)
        return 2
    res = compare(before, after, a.review_pct, a.review_min, a.limit)
    regressions = set(REGRESSIONS) - {c.strip() for c in a.warn_only.split(",") if c.strip()}
    if a.strict:
        regressions |= set(res["findings"])
    res["regressions"] = sorted(c for c in res["findings"] if c in regressions)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str) if a.json else render(res, regressions, a.limit))
    return 1 if res["regressions"] else 0


if __name__ == "__main__":
    sys.exit(main())
