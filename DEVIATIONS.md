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
- **MusicBrainz canonical titles** replace tag titles once a work is matched.
- **`read_error` mappings stay playable.** A tag read failure hides nothing that MA can still play.
- **Album signature skip.** An album whose MA-side signature is unchanged is not re-derived, unless a forced
  reindex is requested.
- **`provider_seen` has an extra `unavailable` state.** A provider that is configured but not loaded hides its
  evidence without retracting it.
- **The scan lists albums straight from MA's library DB.** It runs `SELECT item_id FROM albums WHERE item_id > ?`
  with a resumable cursor, because no paged controller API exists.
- **`classical/reindex` exists already in M1.** It is used by the smoke test and for development.
- **`classical/status` requires LIBRARY_MANAGE**, because it reports household-wide counts.
- **Recordings of one work are listed whole.** The planned split by decade or conductor for large works is M2.
- **Provider tests use a custom in-container harness** instead of MA's pytest fixtures, which are not shipped in
  the image. Status: not written yet (left in M1).
- **No license file yet.**
