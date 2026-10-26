"""Sethster's work-title normalizers (vendored).

From Sethster (sethster/server/sethster_server/canonical.py, by GoatsAndMonkeys), Apache-2.0. Only the
normalizers are kept: `catalogue_key`, `form_key`, `core_title`, `split_excerpt`, `clean_composer`.
Sethster's `build()` union is deliberately not vendored (PLAN.md §5.5): these keys only *retrieve*
merge candidates; identity uses the lossless parser in `catalogue.py`.
"""

from __future__ import annotations

import re

from .text import fold, norm_key

# catalogue prefixes that identify the composer on their own (used to fix composer slips)
CATALOGUE_COMPOSER = {
    "bwv": "bach", "k": "mozart", "kv": "mozart", "d": "schubert", "wab": "bruckner", "hob": "haydn",
    "hwv": "handel", "wwv": "wagner", "s": "liszt", "lw": "liszt", "sz": "bartok", "bb": "bartok",
    "l": "debussy", "rv": "vivaldi", "trv": "strauss", "av": "strauss", "m": "ravel", "lwv": "lully",
    "jw": "janacek", "woo": "beethoven", "h": "berlioz",
}
_CAT_RE = re.compile(
    r"\b(op(?:us)?|bwv|kv?|d|wab|hob|hwv|wwv|s|lw|sz|bb|l|rv|trv|av|m|lwv|jw|woo|h|b)\.?\s*"
    r"([ivxlc]+:\s*)?(\d+[a-z]?)(?:\s*,?\s*no\.?\s*(\d+))?", re.IGNORECASE)
_KEY_RE = re.compile(r"\bin\s+[a-g](?:[\s-]*(?:flat|sharp|♭|♯|b|#))?(?:\s+(?:major|minor))?\b", re.IGNORECASE)
_FORM_RE = re.compile(
    r"^(?P<form>(?:[a-z]+\s){0,3}?(?:symphony|symphonie|sinfonia|concerto|sonata|quartet|quintet|trio|sextet|"
    r"octet|suite|mass|cantata|serenade|divertimento|partita|etude|prelude|nocturne|ballade|scherzo|"
    r"impromptu|rhapsody|overture|string quartet|piano trio|piano sonata|violin sonata|piano concerto|"
    r"violin concerto|cello concerto))\s+no\.?\s*(?P<n>\d+)\b", re.IGNORECASE)
_EXCERPT_RE = re.compile(r"\b(excerpts?|selections?|highlights|aria|duet|trio '|scene|act [ivx\d]+\s*[:,]"
                         r"|movement|mvt|intermezzo from|overture to|prelude to)\b", re.IGNORECASE)
_ARR_RE = re.compile(r"\s*(?:,\s*|\()\s*(?:arr\.?|arranged|transcr\.?|orch\.?|attributed|attr\.?|after|ed\.)"
                     r"[^)]*\)?\s*$", re.IGNORECASE)
_GENERIC = re.compile(
    r"(?:[a-z]+ ){0,3}(?:symphony|concerto|sonata|quartet|quintet|trio|sextet|octet|suite|mass|cantata|"
    r"serenade|divertimento|partita|etude|prelude|nocturne|ballade|scherzo|impromptu|rhapsody|overture|"
    r"fantasia|fugue|toccata|variations|waltz|mazurka|polonaise|sinfonia|motet|aria|song|romance|dance)"
    r"(?: [a-z]+){0,2}?(?: in [a-g](?: flat| sharp)?(?: major| minor)?)?(?: (?:for|no) .*)?")
_UNIDENTIFIED = re.compile(r"\b(unidentified|unknown|encores?\b.*if any|untitled)\b", re.IGNORECASE)


def clean_composer(name: str | None) -> str:
    n = (name or "").strip()
    prev = None
    while prev != n:
        prev = n
        n = _ARR_RE.sub("", n).strip()
    return n or "Unknown"


def catalogue_key(cat: str | None) -> str | None:
    """'Op. 27 No. 2' -> 'op-27-2'; 'K. 533/494' -> 'k-533'; 'Sz. 102, BB 110' -> 'sz-102'."""
    if not cat:
        return None
    m = _CAT_RE.search(fold(cat))
    if not m:
        return None
    pre = m.group(1).lower()
    pre = {"opus": "op", "kv": "k"}.get(pre, pre)
    roman = (m.group(2) or "").strip().rstrip(":")
    parts = [pre] + ([roman] if roman else []) + [m.group(3)] + ([m.group(4)] if m.group(4) else [])
    return "-".join(parts)


def split_excerpt(title: str) -> tuple[str, str] | None:
    """('Così fan tutte', "'Ah guarda, sorella'") for an excerpt title, else None."""
    t = title.strip()
    m = re.match(r"^['\"‘“](.+?)['\"’”]\s+from\s+(.+)$", t, re.IGNORECASE)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^(.+?)\s+from\s+(?:the\s+)?([A-Z].+)$", t)
    if m and not re.search(r"\bsuite\b", m.group(1), re.IGNORECASE):
        return m.group(2).strip(), m.group(1).strip()
    if ":" in _CAT_RE.sub("", t):      # not the colon of a catalogue number (Hob. I:104)
        head, tail = t.split(":", 1)
        if len(head) > 2 and tail.strip():
            return head.strip(), tail.strip()
    m = re.match(r"^(.+?)\s*\(([^)]*\b(?:excerpts?|selections?|closing|opening|scenes?|highlights|"
                 r"movements?|finale)\b[^)]*)\)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def core_title(title: str, strip_quotes: bool = True) -> str:
    t = fold(title)
    t = re.sub(r"\((?:[^)]*\b(?:encore|complete|concert performance|tentative|version|revision|"
               r"orchestral encore)\b[^)]*)\)", " ", t)
    t = re.sub(r"\bcomplete( ballet| opera)?\b", " ", t)
    t = re.sub(r",?\s*\b(op|bwv|k|kv|d|hwv|wwv|hob|wab|rv)(?:\.\s*|\s+)(?:[ivxlc]+:)?\d.*$", " ", t)
    if form_key(title):          # numbered: the key is redundant ("Symphony No. 5" == "... in D minor")
        t = _KEY_RE.sub(" ", t)
    if strip_quotes:             # nicknames (not apostrophes inside words: l'elisir d'amore)
        t = re.sub(r"(?<!\w)['\"‘“].*?['\"’”](?!\w)", " ", t)
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"^(the|der|die|das|le|la|les|il|lo|l'|i)\s+", "", t.strip())
    return norm_key(t)


def form_key(title: str) -> str | None:
    m = _FORM_RE.match(fold(title))
    if not m:
        return None
    form = norm_key(m.group("form"))
    form = form.replace("symphonie", "symphony").replace("sinfonia", "symphony")
    return f"{form} {m.group('n')}"
