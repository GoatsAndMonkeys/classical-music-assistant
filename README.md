# Classical Music Assistant

A classical-music view for [Music Assistant](https://www.music-assistant.io/) (MA). It indexes your library as
**Composer → Work → Recording → movements**, instead of artist → album → track, and adds a companion page for
browsing that way.

It has two parts:

- `provider/classical/`: an MA **plugin provider** (domain `classical`). It hooks into MA's sync, browse,
  recommendations and web server.
- `classical_core/`: a pure-Python package with no MA imports. It holds the tag reader, title parser, name
  resolution, MusicBrainz client and SQLite index. The provider installs it as a wheel.

The project was prompted by MA discussion
[#5481, "Support for Classical Music"](https://github.com/orgs/music-assistant/discussions/5481), and much of
its grouping logic comes from [Sethster](https://github.com/GoatsAndMonkeys/sethster), an earlier classical
player app.

## Status

**Experimental.** It runs on MA **2.10.8** (and one pinned 2.11 nightly) and has been tested on a real personal
library of about 1,000 albums: CD rips, broadcast recordings and messy tags. It has not been tested on other
libraries, with Home Assistant add-on installs, or against real streaming accounts. Expect rough edges, and back
up your MA data directory before trying it.

## Features

- **Its own classical index.** Composers, works and recordings are kept separate from MA's artist list. The
  index reads `COMPOSER`, `WORK`, `MOVEMENT*`, `CONDUCTOR` and `PERFORMER:instrument` tags straight from the
  files with mutagen, because MA's filesystem provider does not keep them.
- **MusicBrainz to fill gaps** (rate-limited to 1 request a second, cached). It resolves composer spellings
  through aliases and life dates; in the test library, six spellings of Beethoven became one composer. Untagged albums are linked to a MusicBrainz release only on a single confident match. A conductor
  named in an album title ("Bernstein: Mahler 2") is not mistaken for the composer.
- **Movements grouped into works**, even on messy rips: operas split across discs, "Act 1 / Duett / Nr. 3"
  style titles, and shorthand like "Brahms PC1".
- **Conservative work links.** A library work is linked to a MusicBrainz work only on a matching catalogue
  number, form + number + key, or a title unique to that composer. Works with several versions (Bruckner
  symphonies, for example) go to review rather than being guessed.
- **Multi-disc sets** ripped as separate albums are joined into one recording when the evidence is strong (same
  work, consecutive discs, same performers, movements that continue). Box sets show as one item with their
  discs and works.
- **A review queue** for anything uncertain. Repeated questions are grouped into one card (for example, a bare
  "Schumann" on several albums: Robert or Clara?), with filters, sorting by how many tracks are affected, bulk
  hide or reject, and undo. Corrections persist across rescans.
- **Browse → Classical** in the normal MA interface, with Composers, Works and Albums folders (and the review
  queue last, for curators).
- **Albums under every composer.** A compilation shows under each composer on it, and album lists can be
  sorted by composer.
- **A companion page** at `/classical/` on MA's web server, with Today, Composers, Works and Performers tabs.
  Curation (the review queue and the edit buttons on pages) sits behind a small **Curate** button at the top,
  so the page stays a plain place to browse and play:
  - Work pages list every recording, with filters by performer and decade, sorting by acclaim, full credits,
    and **Play whole work** (all movements in order, with shuffle off).
  - The Performers tab separates conductors, orchestras, chamber groups, choirs and soloists (with
    instrument), and can show only those who recorded with a given performer.
  - Composition and premiere dates on work pages, with Period and Decade filters on the works list.
  - Liner notes on work pages: the work's English Wikipedia summary and short bios of its composer and the
    recording's performers, reached only through MusicBrainz's own links to Wikidata (no searching), with
    their source and licence. Performer pages show the performer's bio the same way.
  - It works on phones and follows the system light or dark theme.
- **Award badges** from a bundled list of about 2,250 Grammy, Gramophone, Diapason d'Or and BBC Music
  Magazine Award entries. A badge needs the work, the performers and the year to fit; a different conductor
  rules it out. Weaker matches go to review instead of being shown.
- **"Today" shelves** on the MA home page (through MA's recommendations) and on the companion page: premieres
  and anniversaries on this date, the season and the weekday, favouring acclaimed recordings.
- **Streaming albums** in your MA library (Qobuz, Apple Music, Tidal, Spotify and others) are indexed next to
  local files. They are matched through MusicBrainz by barcode or ISRC where possible, so a streamed recording
  lands on the same work as a CD rip. (Untested against real accounts.)
- **MA's permissions are respected**: it uses MA's login, and providers hidden from a user stay hidden.
- Optional extras, all off by default: Wikidata album awards, CritiqueBrainz review excerpts, and a language
  model (Claude or OpenAI-compatible) for composer names nothing else resolves and for a short listening
  guide on each work page, written from the work's facts alone and labelled as AI-written.

## Screenshots

### Classical view inside MA (prototype)

The layout follows the mockups in [#5481](https://github.com/orgs/music-assistant/discussions/5481): Composers,
Works and Performers tabs; work pages whose recordings expand into playable movements; and a performer page
that opens a work filtered to that performer, with **Show all**. It is built in a local branch of MA's own
frontend and runs on this plugin's data. That frontend branch is not published yet, and a stock MA install
won't show this view. The images below come from the real test library while it was still being imported,
so artist photos show initials for now.

<p>
  <img src="docs/screenshots/ma-classical/desktop-work-fidelio.png" width="400" alt="Work page in MA: Fidelio, description, recordings expanding to movements">
  <img src="docs/screenshots/ma-classical/desktop-performers.png" width="400" alt="Performers tab: filter chips for conductors, orchestras, chamber groups, choirs and soloists">
</p>
<p>
  <img src="docs/screenshots/ma-classical/desktop-performer-haitink.png" width="400" alt="Performer page: Bernard Haitink, works performed">
  <img src="docs/screenshots/ma-classical/desktop-work-scoped-haitink.png" width="400" alt="Work page reached from a performer: Showing 4 recordings by Bernard Haitink, Show all">
</p>
<p>
  <img src="docs/screenshots/ma-classical/iphone-work-fidelio.png" width="200" alt="Work page on iPhone">
  <img src="docs/screenshots/ma-classical/iphone-performers.png" width="200" alt="Performers tab on iPhone">
</p>

### Companion page (works with stock MA)

Screenshots are from a synthetic test library and a real personal library, on desktop and iPhone.

<p>
  <img src="docs/screenshots/companion/desktop-light-work-fidelio-liner-notes.png" width="400" alt="Work page: Fidelio, with Play whole work, dates, a Grammy note and liner notes from Wikipedia">
  <img src="docs/screenshots/companion/desktop-light-today.png" width="400" alt="Today: on this day and for the season, each row with a play button">
</p>
<p>
  <img src="docs/screenshots/companion/desktop-light-work-fidelio.png" width="400" alt="Work page: Fidelio, with Play whole work, filters and 29 recordings">
  <img src="docs/screenshots/companion/desktop-light-recording-klemperer-awards.png" width="400" alt="Recording page with movements, credits and a Grammy badge">
</p>
<p>
  <img src="docs/screenshots/companion/desktop-light-performers-with.png" width="400" alt="Performers filtered to those who recorded with one conductor">
  <img src="docs/screenshots/companion/desktop-light-composers.png" width="400" alt="Composers list with life dates and work counts">
</p>
<p>
  <img src="docs/screenshots/companion/desktop-light-ma-home.png" width="400" alt="MA Discover page with On this day and For the season rows">
  <img src="docs/screenshots/companion/desktop-ma-browse-classical.png" width="400" alt="MA Browse, Classical: Composers, Works and Review folders">
</p>
<p>
  <img src="docs/screenshots/companion/iphone-light-work-fidelio-liner-notes.png" width="200" alt="Liner notes on iPhone: Fidelio">
  <img src="docs/screenshots/companion/iphone-light-works.png" width="200" alt="Works on iPhone: dates folded under Period and dates, Curate button in the header">
  <img src="docs/screenshots/companion/iphone-dark-today.png" width="200" alt="Today tab on iPhone, dark theme: premieres on this day">
  <img src="docs/screenshots/companion/iphone-light-review-triage.png" width="200" alt="Review triage on iPhone: one card for an ambiguous composer credit">
</p>

More are in [`docs/screenshots/companion/`](docs/screenshots/companion/) and
[`docs/screenshots/ma-classical/`](docs/screenshots/ma-classical/).

## Install

MA has no supported way to install third-party providers, so installing this is manual. It needs two things:
the `classical_core` wheel in MA's Python environment, and the `provider/classical/` folder inside MA's
`music_assistant/providers/` directory.

1. **Build the wheel** (Python 3.12 or newer):

   ```sh
   python3 -m build --wheel        # or: uv build
   # -> dist/classical_music_assistant-0.1.0-py3-none-any.whl
   ```

2. **Make the wheel installable by MA.** The manifest requires `classical-music-assistant==0.1.0`, and MA
   installs a provider's requirements when it loads it. The package is not on PyPI yet, so either install the
   wheel into MA's environment yourself:

   ```sh
   <MA python> -m pip install --no-deps dist/classical_music_assistant-0.1.0-py3-none-any.whl
   ```

   or point MA's installer at the `dist/` folder with `UV_FIND_LINKS=/path/to/dist`. The package has no
   dependencies of its own; it uses only the standard library plus `aiohttp` and `mutagen`, which MA already
   ships.

3. **Put the provider folder into MA's providers directory.** Find the directory with MA's own Python:

   ```sh
   <MA python> -c "import music_assistant.providers, os; print(os.path.dirname(music_assistant.providers.__file__))"
   ```

   Copy or symlink `provider/classical/` there as `classical/`. In Docker, bind-mount it read-only instead,
   for example:

   ```yaml
   volumes:
     - ./provider/classical:/app/venv/lib/python3.14/site-packages/music_assistant/providers/classical:ro
     - ./dist:/wheels:ro
   environment:
     UV_FIND_LINKS: /wheels
   ```

   A copy inside a container is lost when the container is recreated; a bind mount is not. The Python version
   in that path changes with MA releases, so check it with the command above.

4. **Restart MA**, then add **Classical** under Settings → Providers.

The provider checks MA's version on load and refuses to start on versions it has not been tested with
(currently 2.10.8 and one 2.11 nightly). Set `CLASSICAL_ALLOW_UNTESTED=1` in MA's environment to load it
anyway.

[`docker/compose.dev.yml`](docker/compose.dev.yml) runs pinned MA images with the plugin mounted, and is the
easiest way to try it on a synthetic library:

```sh
python3 scripts/make_fixture_library.py docker/library
python3 -m build --wheel
docker compose -f docker/compose.dev.yml --profile stable up -d    # MA 2.10.8 on port 8095
```

## Usage

1. In the provider's settings, give your email address or website. MusicBrainz requires one, and online
   lookups stay off without it. The other settings work as they are; the optional extras are under
   **Advanced settings**.
2. Let MA sync your library. The plugin indexes tracks as MA imports them; the first pass over a large library
   with MusicBrainz lookups takes a while, because of the 1-request-a-second limit.
3. Open **Browse → Classical** in MA, or the companion page. Its address is shown in the provider's settings;
   it is `/classical/` on your MA server (for example `http://<ma-host>:8095/classical/`) and uses your MA login.
4. To tidy up, open **Curate** at the top of the companion page and work through the review queue. Your
   answers are stored and survive rescans.
5. Play a whole work from its work page, or pick a recording. Playback goes to the MA player you choose at the
   bottom of the page.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]' ruff
pytest -q            # tests/unit (classical_core) and tests/provider (MA glue, with fakes)
ruff check .
node --test "tests/js/*.test.mjs"    # companion page helpers
```

- `classical_core/` has no MA imports and is tested on its own. MusicBrainz responses are replayed from
  `tests/fixtures/mb/` (recorded with `scripts/record_mb_fixtures.py`), so tests do not hit the network.
- `scripts/make_fixture_library.py` builds a synthetic tagged library for the dev containers.
- `scripts/snapshot.py` and `scripts/compare_snapshots.py` snapshot an index and compare two snapshots, to catch
  regressions such as two composers merging into one after a change. See [`docs/deploy.md`](docs/deploy.md).

## Limitations

A plugin can only add things beside MA's existing views. Without changes to MA's core and frontend it cannot:

- add composer, conductor or performer credits to MA's own track and album pages;
- group movements by work on MA's album page;
- add a Classical entry to MA's sidebar (it lives under Browse, on the home page rows and on its own page).
  The prototype above does this through a frontend change;
- be installed from MA's settings. Installing it is manual, as above, and Home Assistant add-on users need
  privileged host access to copy the folder in, which an add-on update removes.

It also only covers items in your MA library. It does not browse a streaming service's whole catalogue.

## Relation to #5481

Discussion [#5481](https://github.com/orgs/music-assistant/discussions/5481) asks for classical support in MA
and includes a proposed design: Composers, Works and Performers tabs, work pages that list recordings, and
performer filters. This plugin is a working prototype of that design within what a plugin can do. It is not
meant as a competing UI. The parts that look reusable upstream are the tag extraction, composer and name
resolution, work grouping (with real-world test cases) and the MusicBrainz work lookups, and the aim is to
offer them to MA core if the maintainers want them.

## License

[Apache License 2.0](LICENSE), the same license as Music Assistant.
