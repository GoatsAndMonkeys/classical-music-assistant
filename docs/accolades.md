# Accolades: awards, reviews, ratings and review links (M3b)

This milestone shows where a recording was acclaimed. It shows award badges, short attributed review excerpts,
community ratings and "Read reviews" links. It also weights the Play pick and the "Most acclaimed" sort toward
acclaimed recordings. The design follows PLAN.md §5.8 (sources) and §5.9 (weighting). The rule above all
others: **never show an award on the wrong recording.** When the evidence is thin, the match goes to the review
queue and nothing is shown.

## Sources and rights

| Source | What it gives | Default | Access and rights |
|---|---|---|---|
| Bundled dataset (`classical_core/data/accolades.json`) | Award winners and Grammy nominations: award, category, year, composer, work, performers (label for Diapason and BBC) | **on** (offline) | Compiled from Wikipedia list articles and BBC Music Magazine's own winner announcements. Each source is cited in the file's `sources` with URL and retrieval date, and each BBC row cites its page. Facts only: no prose is copied. |
| MusicBrainz release-group url relations | Links to AllMusic, Discogs, Wikidata, Wikipedia, review pages | on with the existing MB setting | MB API, 1 request/s, cached (`mb_cache`). Data CC0. |
| Wikidata P166 ("award received") with P585 (point in time) | Album-level awards | off | Wikidata API via the MB link to the item; CC0. Rate-limited, cached in `web_cache`. |
| CritiqueBrainz | Reviews of the release group: rating, reviewer, ≤ 280-character excerpt with its licence and a link | off | Public API. Reviews are CC BY-SA / CC BY-NC-SA. Excerpts are attributed and carry the licence and the link to the full review. |
| Discogs | Community rating (average and count) | off; needs a personal token | Discogs API with `Authorization: Discogs token=…`. Ratings expire after 6 hours (`expires_at`), as the API terms ask. |
| Review sites (Presto Music, Gramophone, MusicWeb International, AllMusic, Discogs) | "Read reviews" links | always | **Links only.** These are search URLs built from composer, work and principal performer, or pages MB links to. Nothing is fetched. |

Not used: any scraping of review sites, the Penguin Guide (no public list), and paid APIs.

## The bundled dataset

There are 2251 entries in 83 categories (341 KB):

| Award | Entries |
|---|---|
| Grammy winners (7 classical categories, 1959 onwards; years are ceremony years) | 272 |
| Grammy nominees (same categories; a nominee row that repeats the winner is dropped) | 1244 |
| Gramophone Award winners (recording categories only; artist/label/lifetime awards dropped) | 501 |
| Diapason d'Or de l'Année winners (from 2014 a line can hold several awards; one the parser cannot split is dropped) | 93 |
| BBC Music Magazine Award winners (9 recording categories plus Recording of the Year, 2007–2026; see below) | 141 |

The rows are `[id, category, year, result, composer, work, performers, label?, url?]`. `url` is the page the
row was read from (BBC rows); without it the category's URL is cited. A BBC row whose page names no label has
`null` there. `categories[i]` gives the
award, the category, its source (an index into `sources`), its Wikipedia URL, and `historical` for
historical/reissue categories. An id is the first 10 hex digits of the sha1 of
`[award, category, year, result, composer, work, performers]`. A rebuild therefore keeps the ids, and the
user decisions keyed on them (`accolade_decision`) survive it.

To rebuild: `python scripts/build_accolades.py --fetch WIKI_DIR`, which downloads the 9 pages' wikitext with
`action=raw` and the BBC pages into `WIKI_DIR/bbc/` (one request every 1.1 s, with a user agent naming the
project). `.cache/` is git-ignored and is the usual `WIKI_DIR`. To re-parse saved pages, drop `--fetch`. The parsers are simple on purpose. A row they cannot
read cleanly is dropped and never guessed. The same goes for a row with no composer, no work, no performer, or
a "composer" that looks like a collection ("Complete works", a volume). The current file is byte-identical to
the script's output.

### BBC Music Magazine Awards

Wikipedia has no per-recording list, so the rows come from the magazine's own winner announcements, one page
per year (2008 and 2009: one page per category). The live pages on classical-music.com are used for 2012,
2014, 2015 and 2019–2026. The older pages (2007–2011 on bbcmusicmagazine.com and classical-music.com, 2016–2018
on awards.classical-music.com) are gone, so their Internet Archive copies are read (`web.archive.org/web/<ts>id_/`),
and the row cites the archived URL. No list of the 2013 winners survives, so 2013 is missing. In 2022 and 2023
the announcement names the winners in prose, so the disc details come from that year's nominee list.

Only recordings of works are kept: the nine recording categories (Orchestral, Concerto, Opera, Choral, Chamber,
Vocal, Instrumental, Premiere, Newcomer) and Recording of the Year. DVD, film, Personality and the other
non-recording awards are skipped. A disc that won two categories gives two rows. A multi-composer disc keeps
every composer: the first is `composer`, and `work` lists the rest as `Work; Other: work`, so it matches at album
level. Performers go through the same normalisation as the other sources (roles and "Sir"/"Dame" dropped,
abbreviations such as LSO expanded, "Orchestra/Conductor" split).

Dropped, and counted by the script (`BBC winners dropped`): 24 winners whose lines cannot be taken apart into
composer, work and performers (run-on text, a heading that names only the artist), and 18 recitals and
anthologies ("works by …", an aria or song disc credited to its singer), which name no one composer's work.

Licence: the rows are facts (award, year, category, composer, work, performers, label). No review or jury text
is copied, and the downloaded pages stay in the git-ignored cache.

BBC awards are announced in spring for discs reviewed in the year before, so their year window is
`[year - 2, year - 1]`, and a session recorded after that (a later concert of the same artists) never matches.

Editable weights, year windows and title/word synonyms live in `classical_core/data/acclaim_tiers.json`.

## Matching (classical_core/accolades.py)

Matching runs offline in the `accolades` job, after the albums and scans. The job compares each entry with
each indexed recording of the same composer (by surname), using these facts:

- **Work.** Folded tokens of the work title. Catalogue numbers and keys must agree exactly. A plural or
  generic title ("Symphonies", "Overtures") never matches a single work. Opera and work names in other
  languages are rewritten through `titles`/`words`, so "The Flying Dutchman" becomes "Der fliegende
  Holländer" and "Symfonie" becomes "Symphony". A nickname in one language only ("From the New World" against
  "Z Nového světa") is ignored when the numbered core agrees.
- **Performers.** These come from the recording's credits, the album's credits and the track-artist strings.
  Each entry performer is matched by surname, and the given name must not conflict ("C. Kleiber" fits
  "Carlos Kleiber" but not "Erich Kleiber"). Ensembles are matched by their distinctive words, and choruses
  only to choruses. A **contradiction** rejects the match outright: a different conductor, or a different
  orchestra, credited where the entry names one. A different name in an artist string only (no credit role),
  while an entry performer is missing, makes the match `uncertain` at best.
- **Years.** Each award has a window for the album's year: Grammy `[year-3, year]`, Gramophone
  `[year-2, year]`, Diapason `[year-1, year]`. A recording made after the award year is never a match. Neither
  is one made more than 10 years before it, except in historical categories. A known session more than two
  years before the window (a reissue of an older recording, or the artists' earlier version) is `uncertain` at
  best, again except in historical categories.

The result is one of four grades:

| Grade | When | Shown as |
|---|---|---|
| `this_release` | exact work, ≥ 2 performers (or the single named performer in full), the principal performer present, album year in the window | the badge, e.g. "Grammy winner 1963" |
| `other_release` | the same, but the album is a later reissue: recorded in or just before the window, released after it. The session year must fit; a matching label or a long cast list is not enough, since the same artists often re-recorded a work for the same label | the badge, marked and noted "awarded to another release of this recording (1962)" |
| `confirmed` | a user chose "Same recording" in the review queue | the badge, noted "match confirmed by a user" |
| `uncertain` | some evidence (principal performer or ≥ 2 performers), not enough | **nothing**; a review item "Possible award: is this the awarded recording?" with the reasons |

User decisions ("Same recording" / "Not this recording") are stored per entry, album and work, and survive
reindexing. Rejected matches are never proposed again.

Entries that cover several works ("Symphonies 5 & 7") attach to the album, not a recording. That happens only
on `this_release` with ≥ 2 performers, when every work the entry names by this composer is on the album (each
number of a numbered plural; at least two works for an unnumbered one such as "Symphonies"). The badge is
noted "for the album" and shows only on the album's recordings that matched one of those works. Network rows are album-level
as well. They count toward a recording's acclaim only when the album holds exactly one recording.

## Weighting and the Play pick

`tier_weight` maps each counted accolade to a weight:

- tier A (major award winners): 2.5;
- B (Diapason d'Or, editor's choices, Wikidata awards): 1.8;
- C (a CritiqueBrainz review rated ≥ 4/5): 1.3;
- D (a Discogs rating ≥ 4/5 from ≥ 5 votes): 1.15;
- a nomination: 1.3.

A recording's `acclaim` is its strongest weight, plus 0.1 × (w − 1) for each further accolade, capped at 2.5.
A recording with nothing weighs 1.0.

`best_pick` (used by `Index.best_recording`, and so by Play and the work page) ranks recordings by:

1. completeness first (a complete recording still beats an acclaimed excerpt);
2. then acclaim;
3. then confirmed status and evidence, as before.

The pick's reason names the award, e.g. "Gramophone Award 1976 (Orchestral)", only when acclaim decided it.
`pick_score(rec, accolades)` is exposed for future curation (radio, playlists). The work page's "Most
acclaimed" sort is enabled when any recording of the work carries a counted accolade.

## Commands and UI

- `classical/accolades` (read): the accolades of one recording or one work.
- `classical/accolade_decide` (manage): `confirm` | `reject` | `clear` on a review-queue match.
- The work and recording views include `accolades`, `acclaim`, `awards`, `reviews`, `ratings`, `links` and
  `review_links`.
- The UI shows badges on recording rows, an acclaim line in the work header, and an "Accolades" section on the
  recording page. That section holds awards, review excerpts with licence and link, ratings, and "Read
  reviews:" links that open in a new tab with `rel="noopener noreferrer"`.

## Configuration

| Setting | Default |
|---|---|
| Show awards from the built-in list | on |
| Look up album awards on Wikidata | off (advanced) |
| Show CritiqueBrainz reviews | off (advanced) |
| Show Discogs community ratings + token | off (advanced) |

Network enrichment runs in chunks of 20 albums after the dataset refresh. It uses the MB contact as its user
agent and the per-host rate limits (Wikidata and CritiqueBrainz 1 request/s, Discogs 1.1 s). Successful
answers are cached for 90 days (Discogs for 6 hours), and an album is enriched again after 30 days. A transient failure stops the chunk and leaves the rest
for the next run.

## Deferred

- **Penguin Guide rosettes**: there is no public list.
- **Preis der deutschen Schallplattenkritik**: its Wikipedia page has no usable per-recording list.
- **The 2013 BBC Music Magazine Awards**: no winner list survives on the magazine's site or in the Internet
  Archive.
- **ICMA (International Classical Music Awards)**: the per-year Wikipedia lists mix layouts (tables, prose,
  missing performers or labels), so there is no clean public list to parse yet.
- **Edison Klassiek and Choc de Classica**: not compiled yet. The tiers file already weights them, so
  adding them is a matter of writing parsers.
- **Gramophone's site search**: its URL is not verified, so the link is a DuckDuckGo `site:gramophone.co.uk`
  search.
- **Wikidata awards**: these carry a generic tier B, because mapping award items to named awards is not done.
- **Composer gaps**: Grammy entries with no composer in the list, mostly recitals and anthologies, are
  dropped.
