"""String normalizers: accent folding, identity keys, slugs, surnames, sort names.

Vendored from Sethster (sethster/server/sethster_server/text.py, by GoatsAndMonkeys), Apache-2.0,
unchanged except that the broadcast-only helpers (track-title cleanup, segment inference) are left out.
"""

from __future__ import annotations

import re
import unicodedata

_TRANSLIT = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
                           "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "ð": "d", "ı": "i"})


def fold(s: str | None) -> str:
    """Lowercase, strip accents, collapse whitespace. Used for matching and search."""
    if not s:
        return ""
    s = s.translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip()


def norm_key(s: str | None) -> str:
    """Aggressive normalization for identity matching: folded, punctuation removed."""
    s = fold(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def slugify(s: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(s).replace("'", "")).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        if "-" in cut[max_len // 2:]:
            cut = cut[: cut.rfind("-")]
        slug = cut.strip("-")
    return slug or "unknown"


_PARTICLES = {"de", "del", "della", "di", "da", "von", "van", "der", "den", "le", "la", "du", "dos", "das", "ter", "ten"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_HONORIFICS = {"sir", "dame", "lord", "lady", "dr", "dr.", "maestro"}


def surname(name: str, particles: bool = True) -> str:
    """'Plácido Domingo' -> 'Domingo'; 'Giancarlo del Monaco' -> 'del Monaco'."""
    name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    parts = [p for p in name.replace(",", " ").split() if p]
    while parts and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    while len(parts) > 1 and parts[0].lower() in _HONORIFICS:
        parts.pop(0)
    if not parts:
        return name
    out = [parts[-1]]
    if particles:
        i = len(parts) - 2
        while i > 0 and parts[i].lower() in _PARTICLES:
            out.insert(0, parts[i])
            i -= 1
    return " ".join(out)


def sort_name(name: str, kind: str = "person") -> str:
    if kind != "person":
        return fold(re.sub(r"^(the|der|die|das|le|la|les|il|l')\s+", "", name, flags=re.IGNORECASE))
    sn = surname(name, particles=False)
    return fold(f"{sn} {name}")


ENSEMBLE_ROLE = re.compile(r"\b(orchestra|chorus|choir|ensemble|band|quartet|trio|quintet|consort)\b", re.IGNORECASE)
ENSEMBLE_NAME = re.compile(
    r"\b(orchestra|orchestre|orchester|orquesta|orchestr\w*|philharmoni\w*|symphony|sinfonia|sinfonietta|"
    r"chorus|choir|chor|coro|choeur|chœur|singers|ensemble|quartet|quartett|quatuor|trio|quintet|consort|"
    r"band|staatskapelle|kapelle|camerata|players|academy of|filarmonica|capella)\b",
    re.IGNORECASE,
)


def is_ensemble(name: str, role: str) -> bool:
    r = role.lower().strip()
    if r in {"orchestra", "chorus", "choir", "ensemble", "children's chorus", "band", "string quartet"}:
        return True
    if ENSEMBLE_ROLE.search(r) and not re.search(r"master|director|leader|soloist|member", r):
        return True
    return bool(ENSEMBLE_NAME.search(name)) and not re.search(r"master|director|conductor", r)
