# Deviations from PLAN.md (revision 8)

Where the MA source in `ref/` (2.10.8 and nightly 2.11.0.dev2026102604) contradicted the plan, the code wins.
Each entry says what the plan asked for, what was built instead, and why.

## Forced by Music Assistant

- **Visibility API differs by version.** Nightly has `helpers.provider_access.visible_music_sources(mass, user)`
  (None means all sources). 2.10.8 lacks it, so the glue falls back to `user.provider_filter` (empty means all).
- **2.10.8 browse neither sorts nor pages provider folders.** The plugin returns lists already ordered and caps
  every non-playable list at 500 entries, splitting larger ones into page folders (`pN`) and composer letter
  folders.
- **Adding a provider goes through a setup flow.** `config/providers/save` only updates an existing instance.
  The smoke script therefore uses `config/providers/setup` and `config/flows/submit`. The Classical plugin has no
  setup flow, so it is created at once with default values.
- **Plugin requirements are installed with `uv pip install pkg==ver`.** For development the core wheel is served
  from a bind-mounted `dist/`, using `UV_FIND_LINKS=/wheels`. This was verified: `classical-music-assistant 0.1.0`
  ends up in `/app/venv`. A real install needs a PyPI release or a vendored copy (M3 packaging).
- **The demo player needs `PYTHONDEVMODE=1`.** `_demo_player_provider` loads only in dev mode, so the compose file
  sets that for the smoke test's player.
- **The 2.10.8 web UI ignores a folder's `version` subtitle.** It shows "folder" instead, so composer dates and
  work/recording counts are visible only through the API and on tracks. The breadcrumb shows raw path segments
  (`c_…`, `w_…`). Both are cosmetic, and for M2 the options are folding counts into names or human-readable path
  segments.
- **Recording folders get their own item_id (`works/<wid>/<rid>`), distinct from their nested path.** That gives
  each one a short, stable play URI (`classical://folder/works/<wid>/<rid>`), while ".." still walks back up the
  nested path.

## Choices made during implementation

- **Tags: mutagen only, no ffprobe fallback.** M0 found that MA's ffprobe-based `async_parse_tags` drops the
  movement and work frames (ID3 MVNM/MVIN; MP4 ©wrk/©mvn/©mvi/©mvc), so the plugin reads tags itself with
  mutagen and nothing else. A file mutagen cannot read is a `read_error` mapping (retried after an hour, it
  retracts nothing and stays playable); formats mutagen does not know get no Tier 1 evidence at all.
- **Composer data:** a bundled composer list (names, dates, sort names) instead of live Wikidata lookups in M1.
- **Schema names and extras:**
  - the table is called `grouping_constraint`;
  - `evidence` has an `edge` column, a stable per-origin key for replacing evidence;
  - the derived caches `review_item`, `album_info` and `track_info` were added.
- **SQLite never runs on the event loop (§5.6).** `store.Database` owns one writer thread with the read-write
  connection (all writes, including the MB cache, go through it, so there is a single writer) and two reader
  threads with read-only WAL connections for browse, search, home rows and commands. `index_album` runs as
  phases: plan and Tier 1 apply on the writer, tag reads in worker threads, MB requests awaited on the loop,
  then one derive transaction on the writer.
- **MusicBrainz needs a contact before it is used.** PLAN §5.6 calls the User-Agent contact a required config
  field, but MA creates the plugin without a setup flow, so a required field would block creation. Instead the
  field stays optional and lookups stay off (with a log warning and a `musicbrainz_note` in `classical/status`)
  until it holds an email address or URL. No placeholder contact is ever sent.
- **MB `Retry-After` is honoured** (seconds or an HTTP date) for every following request. A wait above 60 s
  defers the request instead of blocking: the album keeps no signature and is retried on the next scan, as after
  any transient MB failure (503/429, network errors).
- **Grouping boundary (§5.3).** The lowest common ancestor of adjacent tracks is kept, but the parent walk
  never climbs above a standalone work (MB type opera, symphony, concerto, sonata, mass, song-cycle, suite,
  symphonic poem, ...), into a parent whose catalogue number is a range (BWV 846–869), into an uncatalogued
  parent from a catalogued child, into a parent with a different number in the same catalogue (WWV 86A in
  WWV 86), or into a parent that could not be fetched. So the Ring stays four operas and WTC I stays 24
  prelude-and-fugue pairs.
- **No movement evidence, no grouping (§5.1).** Adjacent tracks of the same work are joined only when each has a
  movement identity, a movement number or an MB partial-performance link; tracks tagged with the work alone
  stay separate recordings and are listed for review.
- **Opera acts count as movement evidence (§5.1).** An act designation in the movement tag or in the title past
  the work ("Tristan und Isolde: Act II", "Akt 3, Szene 1", "Prologue") is a part identity. A track per whole act
  groups with the next act, also across discs; acts going backwards or the same act twice split. Movement
  numbers and names restart per act without splitting ("Scene 1" in Act I and in Act II).
- **Broadcast talk tracks are never works and do not break a performance.** A track without a work whose title
  reads as talk (intermission, interview, commentary, announcer, quiz, synopsis, applause, ...) gets no work,
  even when a composer is credited (Bernstein interviewed between acts), and it is transparent to grouping
  and to the MB grouping boundary: Act I, intermission, Act II is one recording. The tracks either side still
  need movement evidence, so two whole takes with an announcer between them stay two recordings.
- **A forced reindex starts from the first album.** The scan job row records whether it was forced, so an
  interrupted forced scan resumes forced; a normal scan never resumes a forced one as normal.
- **Generic track titles are never works (2026-10).** "Nr. 1", "Act 1", "Track 7", "Rezitativ", "Duett",
  "Arie", "Erster Aufzug", "Overture", bare numbers and the like name a place in a work, not a work. Such a
  track inherits the album's work: the work named in the album title (for one composer, at most four words,
  for example "Fidelio" from "Beethoven: Fidelio Disc 1"), or else the album's single tagged or catalogued
  work. With no such work it gets none, so a mixed-composer album with "01".."06" titles stays raw.
  - On a stage-work album (two or more generic titles, or a stage work in the template), aria incipits with no
    work of their own join the album work too. A lone odd title between two tracks of one opera joins it.
  - Runs of identical titles ("Fidelio, opera, Op. 72" x20) and repeated vague tails ("Continuation" x2)
    are numbered by position, so they are parts of one performance instead of repeated movements.
  - Planned evidence rules are kept: two complete takes of a symphony still split.
- **Local works are keyed per album family.** A work with no MB id or catalogue number is keyed by the album
  title with disc marks removed ("Disc 2", "CD2", "(1)", "Akt II", a trailing digit) plus album artist and
  year. The discs of one set therefore share one work, while each disc album is still its own recording.
  Joining a recording across separate MA albums is not done.
- **Talk tails on tagged tracks.** A track whose work tag is set but whose title tail is broadcast talk
  ("Announcer introduction ...", "host wrap-up") is treated as talk, unless the tail also names music
  ("performance", "end of", "segment", "movement N", ...).
- `DERIVE_VERSION` is 3, so every album re-derives once after this change.
- **MusicBrainz canonical titles** replace tag titles once a work is matched.
- **Name resolution (§5.11) is an addition to the plan.** Composer credits go through a tiered resolver:
  parsing, the authority list, MB artist search, an optional LLM, and review-only clustering, with user
  corrections winning. Identities are stored as evidence on ('name', key).
  - Composers best known as performers are never taken from the artist field. A Bernstein album of his own
    music therefore needs a composer tag.
  - The LLM API key is a `SECURE_STRING` entry. MA 2.10.8 has no `SECRET` entry type.
  - `classical/composer_name` clears every album signature, so the next scan re-derives the whole
    library. This is simple but slow on a large library.
- **`read_error` mappings stay playable.** A tag read failure hides nothing that MA can still play.
- **Album signature skip.** An album whose MA-side signature is unchanged is not re-derived, unless a forced
  reindex is requested.
- **`provider_seen` has an extra `unavailable` state.** A provider that is configured but not loaded hides its
  evidence without retracting it.
- **Live indexing during an MA sync (2026-10).** MA 2.10.8 suppresses per-item `MEDIA_ITEM_*` events while a
  provider sync runs and signals only `MUSIC_SYNC_COMPLETED` at the end, so a first sync left the index empty
  for its whole run (status showed 0 albums while MA held 755 tracks). While `mass.music.active_sync_tasks`
  is non-empty, the glue queues a `changes` job every 20 s. It compares `album_tracks` counts with the
  index's present occurrences and queues the albums that are new or still filling. Metadata-only edits wait for
  the full scan after the sync. Item events are also accepted by their `library://` URI.
- **CUE sheet errors in the MA log are MA's own.** The filesystem provider parses CUE sheets and logs failures
  itself. The plugin only sees the resulting tracks, and does not read tags for CUE track mappings (`::track`).
- **Folder subtitles do not render in the 2.10.8 web UI.** For a `BrowseFolder` the card shows the localized
  name only (no ` - version` suffix) and its second line falls back to the media-type label. That line reads
  `artists`/`authors`/`publisher`/`owner`, which `BrowseFolder` does not carry, so a subtitle would have to be
  folded into the name. Not done yet.
- **The scan lists albums straight from MA's library DB.** It runs `SELECT item_id FROM albums WHERE item_id > ?`
  with a resumable cursor, because no paged controller API exists.
- **`classical/reindex` exists already in M1.** It is used by the smoke test and for development.
- **`classical/status` requires LIBRARY_MANAGE**, because it reports household-wide counts.
- **Recordings of one work are listed whole.** The planned split by decade or conductor for large works is M2.
- **Provider tests use a custom in-container harness** instead of MA's pytest fixtures, which are not shipped in
  the image. Status: not written yet (left in M1).
- **Ambiguous albums are grouped conservatively, not left ungrouped (2026-10).** PLAN §4 says an album with
  missing or duplicate positions is not grouped and goes to review. On the test library that left whole operas
  empty (FIDELIO / FURTWANGLER: CUE tracks of both CDs numbered disc 1; `Leonore - Fidelio (CD2)`). The
  album is still marked ambiguous and listed, but nothing is joined by position: the album's own work (the
  work its name names, which tracks join whatever their order) is one recording in act order, and every other
  identified track is a recording of its own.
- **Coverage fallbacks for albums that derived nothing (2026-10).** For tracks with neither a work tag nor a
  usable title:
  - Text-titled numbers ('Acht, wo weilt sie') join the work the album names, but only when the album names one
    work, by a name rather than a form ('Fidelio', not 'Violin Concerto; Romances').
  - The album's composer may come from an album-name or artist segment ('Opera - Beethoven : Fidelio',
    artist 'Beethoven: Fidelio' or 'Beethoven, Ludwig Van:'), or from a local file's folder names
    ('Beethoven - Fidelio - Furtwangler - EMI 2CDs').
  - Broadcast-log titles: 'Rands--Symphonic Fantasy' names its composer; shorthand such as 'Beethoven 9-1',
    'Brahms PC1' or 'DSCH 13' counts only when the head resolves to a listed composer.
  - A date-prefixed log label ('151017 Elektra BSO') becomes a work only when every track has a composer tag.
  - An album with no recording gets the review item "no work identified on this album". Composer-tagged tracks
    left over get "tracks with a composer but no identifiable work".
  - "possible same work on another album" is one item per new work, listing every match, not one per pair.
- **MusicBrainz release search for albums with no MBIDs (2026-10, beyond §5.11).** An album with a real
  title and artist, and no recording or work MBID anywhere, is searched as a release (`release.py`). One
  request per album, plus one release lookup on a match; all through the 1 req/s cached client. A release
  counts only when it is the single result scoring 95 or more that fits: its title (without disc marker or
  'Composer:' prefix), a credited artist, the per-disc track counts (or one medium for a 'Disc N' album), and
  80 % of track lengths within max(5 s, 5 %). Its recording MBIDs then go through the usual recording →
  work chain. Several fitting releases, or lengths that disagree, give the review item "MusicBrainz release
  match uncertain" and no links.
- **No license file yet.**

## Companion UI (M3, 2026-10)

- **Data goes over `POST /api`, not the websocket.** §6 says the page uses the authenticated websocket.
  `POST /api` runs the same command handlers and scope checks, and plain request/response is simpler for a
  build-free page. The token is MA's own: a login through `../login?return_url=` (returning `?code=`), or the
  `ma_access_token` MA's frontend already stored for the same origin. Borrowing that token is a convenience,
  and the page falls back to its own login.
- **View SQL lives in `classical_core/views.py`, not in new `Index` methods.** This keeps `index.py` untouched
  while it is being reviewed. The views reuse the index's visibility clause, so hidden sources are absent and
  unknown ids return "not found" just as in browse.
- **Not found is a 400, not `MediaNotFoundError`.** 2.10.8's `POST /api` turns `MediaNotFoundError` into a 500
  and logs a traceback, so a stale bookmark or a probed id would flood the log. The view commands raise
  `InvalidDataError("Not found")` instead, with the same text for hidden and unknown ids; the page shows its
  "not found" message for that answer.
- **The login carries a state nonce.** The page sends `return_url=…/classical/?state=<random>` and keeps the
  nonce in `localStorage`; a returning `?code=` is stored only when its `state` matches, so a crafted
  `?code=` link can't sign the page in to another account. Both parameters are stripped at once. Signing out
  of a borrowed MA web token stops borrowing it until the next sign-in on this page.
- **The work and recording views query MA-free SQL of their own.** `Index.recordings` runs one credits query
  per recording (and `Index.recording` lists the whole work to return one), so `views` fetches recordings and
  all their credits in two queries.
- **The static route is registered with method `"*"`.** The handler answers only GET and HEAD (405 otherwise).
  MA's prefix match means `/classicalfoo` also reaches it, so it returns 404 for anything outside `/classical/`.
  A route left over from a crashed reload is replaced once.
- **Corrections need LIBRARY_MANAGE.** The UI calls the existing `classical/merge_composers` and
  `classical/composer_name` commands, and hides those controls when `classical/ui_info` says the user lacks the
  scope. The server still enforces the scope.
- **"Most acclaimed" sorting is a disabled placeholder.** Recordings carry an empty `accolades` list until award
  data exists.
- **Composed-date filters return nothing yet.** The index stores no composition dates, so the Works filter
  reports `date_coverage.composed = 0`. Recorded and released years work.
- **MA's remote (WebRTC) mode is not supported.** The page needs to reach MA over HTTP (LAN, reverse proxy or HA
  ingress).
- **The work page's recording list is compact.** Credits omit `kind`, and `instrument` appears only when it is
  set. The page derives performer ids from the credits. This keeps a 400-recording work under 400 KB of JSON.
