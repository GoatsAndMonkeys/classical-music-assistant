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
  The discs are then joined into one recording when the evidence allows (see "Cross-disc recordings" below).
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
- **Works merge across albums on strong evidence (2026-10, changes §5.4/§5.5).** §5.5 lists title-derived
  works of different albums as candidates and never joins them. On the test library that left 25 "Fidelio"
  works. A title-derived work now joins the same work on another album when the composer resolves to the same
  person and either:
  - the full catalogue number matches (Op. 72a is not Op. 72; Op. 59 No. 1 is not No. 2), the titles agree
    (numbers, form, key) and the keys do not conflict; or
  - both titles name the same entry of a short list of distinctive works (`work_identity.py`: Fidelio,
    Der fliegende Holländer, Leonore, Alexander Nevsky…). Generic titles ('Violin Concerto') never merge.
    Brackets naming another piece ('The Firebird (1919 Suite)', 'Fidelio [Excerpts]') keep it apart, every
    name word must match on both sides ('Prelude' is not 'Prelude and Liebestod'), and an arrangement
    (an arranger tag, 'orch. Ravel' in the title, a MusicBrainz arrangement) never merges.

  Two different MusicBrainz works never merge, and a MusicBrainz work keeps its own title. A tag-catalogue
  match against a MusicBrainz work now merges when the titles agree, instead of always being a conflict.
  The absorbed work's id redirects to the survivor. Same number with titles that disagree gives the review
  item "same catalogue number, different title"; title-only pairs stay "possible same work on another album".
  A recording page offers "Keep separate" (`classical/separate_work`, LIBRARY_MANAGE), which undoes that
  album's merge and keeps it apart on every later scan. The work type takes the best evidence of the merged
  works (MusicBrainz or the list, then title words, then act names).
- **A given name copied from the work title is corrected (2026-10, changes §5.11 d).** §5.11 (d) keeps
  'Alexander Prokofiev' for review only. When the single given name is a word
  of a listed work of that composer ('Alexander Nevsky, Op. 78') named in a title on the same album, the
  name is never followed there by the surname, the surname is unique in the list and not shared by other
  notable composers (Mozart, Haydn), the person is not a known namesake (Siegfried Wagner) and the life dates
  agree, the credit resolves to the listed composer (Sergei Prokofiev). The review item
  "composer name corrected from the work title" records it. Otherwise the review item is "suspicious composer
  name" and nothing changes.
- **Nested performer credits keep the person once (2026-10).** 'Ain Anger (bass (Pater Profundus))' is the
  person Ain Anger, role bass, detail 'Pater Profundus'. The detail is stored per track credit and shown after
  the role. Stored tags from older scans are re-parsed the same way.
- **Movement names prefer the file name over "Part n" (2026-10).** When every track of a work has the same
  title and no movement tags, the file names give the movement names ('01-Overture.mp3' → 'Overture') if
  they are all different. Otherwise a movement name is the movement tag, else the track title without the
  work's name.
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
- **"Most acclaimed" sorting is enabled only for works with acclaim.** It is disabled ("no awards found") when no
  recording of the work carries a counted accolade (docs/accolades.md).
- **Accolades are matched against a bundled, Wikipedia-compiled list, not live award sites.** Network sources
  other than MusicBrainz (Wikidata, CritiqueBrainz, Discogs) are opt-in, and review sites are links only. Award
  badges need work, performer and year agreement. A reissue is labelled "another release of this recording",
  and anything weaker waits in the review queue (docs/accolades.md).
- **Composed-date filters return nothing yet.** The index stores no composition dates, so the Works filter
  reports `date_coverage.composed = 0`. Recorded and released years work.
- **MA's remote (WebRTC) mode is not supported.** The page needs to reach MA over HTTP (LAN, reverse proxy or HA
  ingress).
- **The work page's recording list is compact.** Credits omit `kind`, and `instrument` appears only when it is
  set. The page derives performer ids from the credits. This keeps a 400-recording work under 400 KB of JSON.
- **Themed curation (§5.9) is a simpler, stateless first cut.** `classical_core/curation.py` builds four shelves
  (On this day, For the season, For <weekday>, Acclaimed recordings) on demand. There is no `themes.yaml`, no
  stored `theme_day` assignments, no `classical://playlist/today` and no household spacing. Picks are seeded
  by the date (sha256 of date, shelf and work), so the same day gives the same shelves for every user with the
  same visible sources. The cache key is date, visibility and hemisphere, for 5 minutes (eight keys kept; one
  computation per key at a time; a stale entry is served while it refreshes). The companion UI sends the
  browser's local date. MA's home rows can't carry one, so they use the MA server's local date: set `TZ` on
  a container, which is otherwise UTC. Weights come from the
  accolades `pick_score` plus evidence quality (complete, matched recordings first). There is one composer per
  shelf, and a work shows only once across shelves, even when it is indexed twice. The rules are in the spirit
  of Sethster's, written fresh: liturgical Sundays (Bach cantatas), Mozart/Messiah Mondays, Concerto Tuesdays,
  Halloween and "Optoberfest", a hemisphere option (default north) and a bundled `premieres.json` list
  (Wikipedia-sourced). US civic holidays are left out. The companion UI opens on a new Today tab, and MA's
  home shows the same shelves as `today/<id>` recommendation rows.

## Streaming albums in the library (§5.10, 2026-10-27)

- **What a service says is fallback evidence, never a tag.** A streaming track has no file, so its composers and
  credits come from MA's `Track.metadata.performers` and `artists`. They are stored as their own entity
  (`provider_track`, origin `provider:<track>`), used only when the track has no file tags, and never raise a
  "conflicting tags" review. Each service fills `performers` differently: Qobuz gives "Name, Role" credits,
  Apple Music the composer field, Tidal/Subsonic bare names, and Spotify/Deezer/YouTube nothing. With nothing
  usable, the track is treated as having no composer and is not indexed as classical. No guessing.
- **MA 2.10.8 splits Qobuz credits on every '-'.** As a result, "Jean-Efflam Bavouzet, Piano" arrives as an
  unordered set: {"Jean", "Efflam Bavouzet, Piano"}. When any fragment shows this damage, a credit is kept only
  in two cases:
  - an MA artist confirms it, exactly or as a hyphen tail;
  - for composers, a listed composer confirms it, either as given or rejoined with a fragment
    ("Camille Saint" + "Saëns").
- **Barcode and ISRC lookups apply only to remote items.** A streaming-only album with a UPC/EAN looks it up on
  MusicBrainz before the title search. It is accepted only when every release with that barcode that fits the
  disc layout gives the same recordings, and the track lengths agree. Otherwise it goes to review ("releases with
  this barcode disagree").
- **ISRCs are a last resort, after tags, MA's MBID and the release match.** An ISRC with one recording of
  fitting length is taken. With 2–3, they are taken only when MB links them all to the same works; any other
  case is reviewed ("MusicBrainz ISRC match uncertain").
- **Local albums' signatures and lookups are unchanged by any of this.**
- **A shared title prefix no longer keeps a leading composer.** Streaming services title tracks "Beethoven:
  Symphony No. 5 in C Minor, Op. 67: I. …". The run prefix now drops a leading "<composer>: " only when that
  head names the track's own composer (full name, surname, or a name that resolves to them), so the work
  title and key match the local copy's. "Don Giovanni: Act 1", "Pulcinella: …" and Liszt's "Beethoven:
  Symphony No. 5" transcription keep their heads. This changes derivation for local files titled the same
  way, so `DERIVE_VERSION` is now 7. Tags are not re-read.
- **A composer best known as a performer (Mahler, Bernstein) is not a roleless performer credit** unless a
  listed composer is also among the track artists: Spotify lists "Gustav Mahler" first on his own symphonies.
- **Provider badges.**
  - A recording lists the visible provider instances of its tracks.
  - A work whose visible recordings are all local sends no sources at all, so local-only libraries look and
    weigh as before. The 400-recording view stays under its size budget.
  - Local files get a badge only beside a streaming copy.
- **"Other recordings on <service>" searches each provider instance itself** (`prov.search`, tracks, limit 50,
  20 s timeout). MA's global search keeps only one instance per streaming domain, so it is not used.
  - Results are grouped by album. A track must name the work (by catalogue number when the work has one) and the
    composer's surname must appear on the track or its album.
  - Albums with any track already in the library are dropped.
  - The search runs only on a button press, and only for the caller's visible services that can search.
- **"Add to library" is `classical/add_album`, which needs `LIBRARY_WRITE`** (as MA's `music/library/add_item`;
  `ui_info.can_add`). It takes a provider instance and album id, accepts only the caller's visible, searchable
  streaming services, and calls MA's `add_item_to_library`. MA adds no album tracks unless the provider's
  "import album tracks" option is on (off by default), and the index reads library tracks, so the command
  imports them (`import_album_tracks`) when the option is off. Indexing then follows MA's normal events;
  nothing is added implicitly.
- **Deferred.**
  - §5.10's catalogue model for items that are not in the library: suggestions are not stored and not browsable
    in MA's folders.
  - Promoting a streaming recording to replace a local one.
  - Per-service credit parsing beyond the five shapes above.
  - A live test against a real streaming account (the M1 has none). Fixtures cover Qobuz, Apple Music and
    Spotify.

## Cross-disc recordings

- **One recording can span several MA albums** when a multi-disc set was ripped as one album per disc
  (`classical_core/discs.py`, migration 0004). Each disc is still derived on its own. The first disc's
  recording becomes the lead and takes the later discs' slots; a follower keeps its row with `joined_to`
  and a `cross-disc` redirect, so its id still resolves. Slots keep `home_id`/`home_ord`, and the follower's
  own fields are kept in `own_json`, so an unjoin restores everything exactly. Play order is disc, then track,
  so "play whole work" runs across the discs.
- **Which albums are a set.** Albums are candidates when they share a set key (album title with the disc mark
  removed, plus album artist), a tagged MusicBrainz release id, or an enriched MB release. A user's
  "together" link also adds an album. Disc numbers come from the title mark ("Disc 2 of 3", "CD2", "(2)",
  "Fidelio1") or the DISCNUMBER/DISCTOTAL tags; the title wins over a tag that says 1, and any other
  conflict leaves the disc unknown. Sets of more than 30 albums are not joined.
- **Evidence rules for a pair (earlier disc A, later disc B).** A pair joins only when it passes every check:
  - the same work;
  - consecutive disc numbers with the same total, and no disc above the total;
  - the same year, label, release id and album artist where both have them;
  - A's recording ends its disc and B's starts its disc, and neither is already complete;
  - no shared movement identity (MB work, tagged part, title part), except a single number split
    across the disc break;
  - movement numbers continue instead of restarting, and opera acts do not go backwards;
  - no more parts than the work's total;
  - performers agree: conductor and orchestra must overlap where both sides list them, and soloists when
    neither side lists a conductor or orchestra;
  - recorded years at most one apart.
  A pair that fails a hard check is never joined, so a duplicated disc stays separate. A pair that passes
  the hard checks but has a doubt goes to review as "possible continuation on another disc" and is not joined.
  Doubts are: no disc numbers, no shared performers, no movement identities, a gap in movement numbers, or a
  split number. Each disc has at most one successor and one predecessor.
- **User corrections win and survive re-index.** `classical/join_discs` (from the review item: "Same recording:
  join the discs" / "Different recordings") stores a `disc_pair` override, the latest for each pair.
  `classical/separate_discs` ("Keep discs separate" on the recording page) stores "separate" for each
  adjacent pair. Both are logged in `correction_log`. A forced join still refuses different works, and still
  needs the earlier recording to end its disc and the later one to start its disc.
- **No guessed joins between copies or sources.** When the evidence offers a disc more than one continuation
  (two copies of a set as separate albums), none of them is joined by the evidence; each pair is queued, and a
  user's join settles the rest. Discs whose tracks do not all play from the same provider instances are queued
  too, since a joined recording is shown only to a user who can play every disc. A later disc's recording id
  (browse path, play URI) opens the joined recording. An award on the recording and on a later disc's album
  shows once.
- **Incremental.** Deriving or removing an album unjoins every group it touches, then re-joins that album's
  set, so a disc that arrives later joins the existing recording. The `accolades` job is queued after a
  correction. Only the lead recording is visible; follower albums still show their award badges, without
  duplicates.
- `DERIVE_VERSION` is now 9 (8 was the name-parsing fixes; the two merged into one bump). Album evidence (`set_key`, `disc_no`,
  `disc_total`, `release_mbid`) and the new slot columns are only written by a derive, so every album
  re-derives once. DISCTOTAL and MUSICBRAINZ_ALBUMID are new tag fields. They are read only when a file's
  tags are next read, but disc numbers from titles and DISCNUMBER work at once.
- **Not done.** An MB release found later by enrichment does not trigger a re-join until the album next
  re-derives. A user's award decision is keyed by the lead recording's album.

## Work dates (2026-10-27)

Work pages say "composed 1804–05 · premiered 1805", with the source in small muted text. The Works list can
filter by period (Early, Baroque, Classical, Romantic, 20th c., Contemporary) and decade, and sort by date.
"On this day" uses the stored premiere.

- **Sources, highest first.** The sources (`classical_core/dates.py`, schema v5) are:
  1. A user correction (`classical/work_dates`, "Edit dates" on the work page). It is an `override` row
     (op `dates`) and is logged in `correction_log`, so it survives re-index. "unknown" hides an automatic
     value; a blank field goes back to the automatic sources.
  2. An explicit COMPOSITIONDATE tag, taking the commonest readable value across the work's files.
  3. MusicBrainz, only for works that are already MB-linked. It reads the composer relationship's
     begin/end; a work without its own dates takes the span of its cached parts, three levels down (Fidelio
     gives 1805–1814). Premieres come from the earliest premiere event that was not cancelled. There is no
     search.
  4. The bundled `premieres.json`. It is used only for an "original" work of the same type with exactly one
     matching date. A title that joins works at a top-level ";" or " / " ("Blanc et noir; Prélude à
     l'après-midi d'un faune") matches only if every part does. Suites, excerpts, highlights, scenes, acts,
     preludes, overtures, arias, arrangements, fantasies and variations ("Parsifal – Prelude to Act 1 and
     Good Friday music") do not take the entry's premiere unless the entry is one too. "On this day" applies
     the same test (`dates.dataset_hit`) when it falls back to the list for a work the dates job left
     without a premiere.

  ORIGINALDATE, recording years and release years are never used. Each source's answer is a `work_date` row
  holding a partial ISO date and a precision (day, month, year, decade or circa). The chosen value and its
  source go into `work`. For a premiere, a lower source that agrees on the year but is more precise wins
  (MB "1805" plus the list's "1805-11-20").
- **The job.** A `dates` job runs after album jobs and scans. It reads tags, the dataset and the MB cache,
  writes only changes, and remembers when it last read each work's MB answer (`dates_mb_at`). The dates
  answer and the indexer's work answer are read together (only the latter lists an opera's parts). Changing
  `dates.MB_READER` re-reads every cached answer once. If MusicBrainz
  is on, a `dates_net` job then fetches `artist-rels+event-rels` for linked works, 10 at a time, at the
  client's 1 request a second, through the cache. It skips works whose dates are settled by the user or by
  tags, and stops on a transient error. With MusicBrainz off, only answers already cached are used. No
  DERIVE_VERSION bump.
- **Lists.** A work's list year (period, decade, date sort) is the first year it was composed. Without one,
  it is the premiere year only when the composer is a known one who was alive within a year of the premiere
  (or is living); otherwise the work stays undated, so a posthumous premiere (Schubert's Unfinished, 1865)
  does not put a work in the 1860s. A composition range matches any year range it overlaps. "premiered" is a
  new date kind. Undated works sort last.
- **Hyphenated two-digit years.** For a composition date (tags and the edit sheet), "YYYY-NN" with an ASCII
  hyphen is a range when NN is later than the year's own last two digits: "1804-05" is 1804–1805 and
  "1805-11" is 1805–1811. Otherwise NN is a month ("1898-03" is March 1898); "1850-20" is refused. A
  premiere is one date, so there "1805-11" is November 1805. Full dates ("1805-11-20") are unaffected.
- **Not done or known gaps.**
  - Existing files need a forced reindex before COMPOSITIONDATE is read.
  - The dataset's patterns are still loose within one title: an excerpt named without any of the words
    above (an aria by its first line) can take the opera's premiere.
  - The dates job fetches no uncached parts.
  - A composition tag "1805-11" meant as November 1805 reads as 1805–1811.
