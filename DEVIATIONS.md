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

- **Tags: mutagen first, ffprobe as fallback.** M0 found that MA's ffprobe-based `async_parse_tags` drops the
  movement and work frames (ID3 MVNM/MVIN; MP4 ©wrk/©mvn/©mvi/©mvc), so the plugin reads tags itself with
  mutagen.
- **Composer data:** a bundled composer list (names, dates, sort names) instead of live Wikidata lookups in M1.
- **Schema names and extras:**
  - the table is called `grouping_constraint`;
  - `evidence` has an `edge` column, a stable per-origin key for replacing evidence;
  - the derived caches `review_item`, `album_info` and `track_info` were added.
- **SQLite runs on the event loop.** Queries are small and indexed. A worker thread is deferred until profiling
  shows a need.
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
