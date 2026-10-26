#!/usr/bin/env python3
"""Generate the synthetic classical fixture library (PLAN.md §8 "Fixture library").

Short sine-wave files made with ffmpeg and tagged with mutagen, in three tag styles:
full Picard tags (FLAC), ID3 movement frames (MP3), MP4 atoms (M4A), plus bare-title
files with no classical tags at all. Albums:

    karajan_5_7/     FLAC, full Picard: Beethoven Symphonies 5 & 7, Karajan/BPO, recorded 1962
    kleiber_5/       MP3, ID3 TIT1/MVNM/MVIN/TPE3: Beethoven 5, Kleiber/VPO, rec. 1974, released 1995
    marriner_40/     M4A, ©wrk/©mvn/©mvi atoms: Mozart Symphony 40, Marriner/ASMF, no recording date
    bare_brahms/     FLAC, bare titles only ("Symphony No. 1 in C minor, Op. 68: I. Un poco sostenuto")
    opera_2cd/       FLAC, a two-disc opera whose movement numbers continue across discs
    takes_box/       FLAC, one work with an alternate take of the finale (must split)
    compilation/     MP3, several composers, single-movement works
    pop/             MP3, a non-classical track (must not appear)

Usage: make_fixture_library.py OUT_DIR [--seconds N]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3,
    MVIN,
    MVNM,
    TALB,
    TCOM,
    TDRC,
    TIT1,
    TIT2,
    TPE1,
    TPE2,
    TPE3,
    TPOS,
    TRCK,
    TXXX,
)
from mutagen.mp4 import MP4, MP4FreeForm

FREQS = [220, 247, 262, 294, 330, 349, 392, 440, 494, 523]


def sine(path: Path, codec: str, seconds: float, idx: int) -> None:
    """Write a short sine tone in the given container."""
    path.parent.mkdir(parents=True, exist_ok=True)
    freq = FREQS[idx % len(FREQS)]
    args = {
        "flac": ["-c:a", "flac"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "96k"],
        "m4a": ["-c:a", "aac", "-b:a", "96k"],
    }[codec]
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
            "-ac", "2", *args, str(path),
        ],
        check=True,
    )


def tag_flac(path: Path, tags: dict[str, str | list[str]]) -> None:
    f = FLAC(path)
    f.delete()
    for k, v in tags.items():
        f[k] = v if isinstance(v, list) else [v]
    f.save()


def tag_mp3(path: Path, t: dict[str, str | list[str]]) -> None:
    id3 = ID3()
    simple = {"title": TIT2, "album": TALB, "artist": TPE1, "albumartist": TPE2,
              "composer": TCOM, "conductor": TPE3, "date": TDRC, "work": TIT1}
    for key, frame in simple.items():
        if key in t:
            id3.add(frame(encoding=3, text=t[key]))
    if "tracknumber" in t:
        id3.add(TRCK(encoding=3, text=t["tracknumber"]))
    if "discnumber" in t:
        id3.add(TPOS(encoding=3, text=t["discnumber"]))
    if "movementname" in t:
        id3.add(MVNM(encoding=3, text=t["movementname"]))
    if "movement" in t:
        id3.add(MVIN(encoding=3, text=f"{t['movement']}/{t.get('movementtotal', '')}".rstrip("/")))
    for key in ("performer", "originaldate", "musicbrainz_workid"):
        if key in t:
            desc = {"musicbrainz_workid": "MusicBrainz Work Id", "originaldate": "originaldate",
                    "performer": "PERFORMER"}[key]
            id3.add(TXXX(encoding=3, desc=desc, text=t[key]))
    id3.save(path)


def tag_m4a(path: Path, t: dict[str, str | list[str]]) -> None:
    f = MP4(path)
    f.delete()
    atoms = {"title": "\xa9nam", "album": "\xa9alb", "artist": "\xa9ART", "albumartist": "aART",
             "composer": "\xa9wrt", "date": "\xa9day", "work": "\xa9wrk", "movementname": "\xa9mvn"}
    for key, atom in atoms.items():
        if key in t:
            f[atom] = [t[key]] if isinstance(t[key], str) else t[key]
    if "tracknumber" in t:
        n, _, tot = str(t["tracknumber"]).partition("/")
        f["trkn"] = [(int(n), int(tot or 0))]
    if "discnumber" in t:
        n, _, tot = str(t["discnumber"]).partition("/")
        f["disk"] = [(int(n), int(tot or 0))]
    if "movement" in t:
        f["\xa9mvi"] = [int(t["movement"])]
    if "movementtotal" in t:
        f["\xa9mvc"] = [int(t["movementtotal"])]
    if "conductor" in t:
        f["----:com.apple.iTunes:CONDUCTOR"] = [MP4FreeForm(str(t["conductor"]).encode())]
    f.save()


TAGGERS = {"flac": tag_flac, "mp3": tag_mp3, "m4a": tag_m4a}

SYM5 = ["Allegro con brio", "Andante con moto", "Scherzo. Allegro", "Allegro"]
SYM7 = ["Poco sostenuto – Vivace", "Allegretto", "Presto", "Allegro con brio"]
SYM40 = ["Molto allegro", "Andante", "Menuetto. Allegretto", "Allegro assai"]
BRAHMS1 = ["Un poco sostenuto – Allegro", "Andante sostenuto", "Un poco allegretto e grazioso",
           "Adagio – Più andante – Allegro non troppo, ma con brio"]
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]


def albums() -> list[dict]:
    out: list[dict] = []

    # 1. full Picard tags, two works on one album
    tracks = []
    for wi, (work, movs, wid) in enumerate([
        ("Symphony No. 5 in C minor, Op. 67", SYM5, "c4a3a9f7-0000-4000-8000-000000000005"),
        ("Symphony No. 7 in A major, Op. 92", SYM7, "c4a3a9f7-0000-4000-8000-000000000007"),
    ]):
        for mi, mov in enumerate(movs):
            tracks.append({
                "title": f"{work}: {ROMAN[mi]}. {mov}", "work": work, "movementname": mov,
                "movement": str(mi + 1), "movementtotal": str(len(movs)),
                "musicbrainz_workid": wid,
                "composer": "Ludwig van Beethoven", "conductor": "Herbert von Karajan",
                "performer": ["Berliner Philharmoniker (orchestra)"],
                "artist": "Herbert von Karajan; Berliner Philharmoniker",
            })
    out.append({"dir": "karajan_5_7", "codec": "flac", "album": "Beethoven: Symphonies Nos. 5 & 7",
                "albumartist": "Herbert von Karajan", "date": "2002", "originaldate": "1962",
                "tracks": tracks})

    # 2. ID3 movement frames; release 1995, recorded 1974 (originaldate)
    out.append({"dir": "kleiber_5", "codec": "mp3", "album": "Beethoven: Symphony No. 5",
                "albumartist": "Carlos Kleiber", "date": "1995", "originaldate": "1974",
                "tracks": [{
                    "title": f"Symphony No. 5 in C minor, Op. 67: {ROMAN[i]}. {m}",
                    "work": "Symphony No. 5 in C minor, Op. 67", "movementname": m,
                    "movement": str(i + 1), "movementtotal": "4",
                    "composer": "Ludwig van Beethoven", "conductor": "Carlos Kleiber",
                    "performer": "Wiener Philharmoniker (orchestra)",
                    "artist": "Wiener Philharmoniker, Carlos Kleiber",
                } for i, m in enumerate(SYM5)]})

    # 3. MP4 atoms; no recording date at all, only the release date
    out.append({"dir": "marriner_40", "codec": "m4a", "album": "Mozart: Symphony No. 40",
                "albumartist": "Academy of St Martin in the Fields", "date": "2010",
                "tracks": [{
                    "title": f"Symphony No. 40 in G minor, K. 550: {ROMAN[i]}. {m}",
                    "work": "Symphony No. 40 in G minor, K. 550", "movementname": m,
                    "movement": str(i + 1), "movementtotal": "4",
                    "composer": "Wolfgang Amadeus Mozart", "conductor": "Neville Marriner",
                    "artist": "Academy of St Martin in the Fields, Neville Marriner",
                } for i, m in enumerate(SYM40)]})

    # 4. bare titles: composer only in the artist field (Tier 3, composer authority list)
    out.append({"dir": "bare_brahms", "codec": "flac", "album": "Brahms: Symphony No. 1",
                "albumartist": "Johannes Brahms", "date": "1990",
                "tracks": [{
                    "title": f"Symphony No. 1 in C minor, Op. 68: {ROMAN[i]}. {m}",
                    "artist": "Johannes Brahms",
                } for i, m in enumerate(BRAHMS1)]})

    # 5. two-disc opera, numbering continues across discs
    acts = [("Overture", 1), ("Act I: No. 1 Duettino", 1), ("Act I: No. 2 Duettino", 1),
            ("Act II: No. 10 Cavatina", 2), ("Act II: No. 11 Arietta", 2), ("Act II: Finale", 2)]
    out.append({"dir": "opera_2cd", "codec": "flac", "album": "Mozart: Le nozze di Figaro (highlights)",
                "albumartist": "Karl Böhm", "date": "1999", "originaldate": "1968",
                "tracks": [{
                    "title": f"Le nozze di Figaro, K. 492: {name}", "work": "Le nozze di Figaro, K. 492",
                    "movementname": name, "movement": str(i + 1), "movementtotal": "6",
                    "composer": "Wolfgang Amadeus Mozart", "conductor": "Karl Böhm",
                    "performer": ["Orchester der Deutschen Oper Berlin (orchestra)",
                                  "Hermann Prey (baritone)", "Edith Mathis (soprano)"],
                    "artist": "Karl Böhm", "discnumber": f"{disc}/2",
                } for i, (name, disc) in enumerate(acts)]})

    # 6. alternate take of the finale on the same album: must split the run
    movs = ["Allegro", "Adagio", "Rondo. Allegro", "Rondo. Allegro (alternate take)"]
    nums = [1, 2, 3, 3]
    out.append({"dir": "takes_box", "codec": "flac", "album": "Haydn: Trumpet Concerto (session takes)",
                "albumartist": "Wynton Marsalis", "date": "2004", "originaldate": "1982",
                "tracks": [{
                    "title": f"Trumpet Concerto in E-flat major, Hob. VIIe:1: {ROMAN[n - 1]}. {m}",
                    "work": "Trumpet Concerto in E-flat major, Hob. VIIe:1", "movementname": m,
                    "movement": str(n), "movementtotal": "3", "composer": "Joseph Haydn",
                    "conductor": "Raymond Leppard", "performer": ["Wynton Marsalis (trumpet)",
                                                                  "National Philharmonic Orchestra (orchestra)"],
                    "artist": "Wynton Marsalis",
                } for m, n in zip(movs, nums, strict=True)]})

    # 7. compilation, several composers, one track per work
    out.append({"dir": "compilation", "codec": "mp3", "album": "Adagio Classics", "albumartist": "Various Artists",
                "date": "2005", "tracks": [
                    {"title": "Canon and Gigue in D major, P. 37: Canon", "work": "Canon and Gigue in D major, P. 37",
                     "movementname": "Canon", "movement": "1", "movementtotal": "2",
                     "composer": "Johann Pachelbel", "artist": "English Chamber Orchestra"},
                    {"title": "Adagio for Strings, Op. 11", "work": "Adagio for Strings, Op. 11",
                     "composer": "Samuel Barber", "conductor": "Leonard Slatkin",
                     "artist": "Saint Louis Symphony Orchestra"},
                    {"title": "Air on the G String", "composer": "Johann Sebastian Bach",
                     "work": "Orchestral Suite No. 3 in D major, BWV 1068", "movementname": "Air",
                     "movement": "2", "movementtotal": "5", "artist": "Academy of Ancient Music"},
                ]})

    # 8. non-classical
    out.append({"dir": "pop", "codec": "mp3", "album": "Summer Hits", "albumartist": "The Synths", "date": "2021",
                "tracks": [{"title": "Dancing in the Static", "artist": "The Synths"}]})
    return out


def build(out_dir: Path, seconds: float) -> int:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    n = 0
    for album in albums():
        codec = album["codec"]
        per_disc: dict[str, int] = {}
        for i, t in enumerate(album["tracks"]):
            tags = dict(t)
            disc = str(t.get("discnumber", "1")).split("/")[0]
            per_disc[disc] = per_disc.get(disc, 0) + 1
            tags.setdefault("album", album["album"])
            tags.setdefault("albumartist", album["albumartist"])
            tags.setdefault("date", album["date"])
            if "originaldate" in album:
                tags.setdefault("originaldate", album["originaldate"])
            tags.setdefault("tracknumber", str(per_disc[disc]))
            ext = codec
            path = out_dir / album["dir"] / f"{i + 1:02d}.{ext}"
            sine(path, codec, seconds, n)
            TAGGERS[codec](path, tags)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    n = build(a.out_dir, a.seconds)
    print(f"wrote {n} tracks to {a.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
