// Pure helpers of the companion UI (provider/classical/web/app.js): node --test "tests/js/*.test.mjs"
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const APP = new URL("../../provider/classical/web/app.js", import.meta.url);
const app = require(APP.pathname);

const rec = (id, o = {}) => ({ id, label: id, who: o.who || id, year: o.year ?? null, year_kind: o.kind ?? null,
  decade: o.year ? Math.floor(o.year / 10) * 10 : null, release_year: o.rel ?? null, duration: o.dur || 0,
  album: o.album || "", credits: o.credits || [],
  play_uri: `classical://folder/works/w/${id}`, movements: o.movements });

test("hash routes parse and format round-trip", () => {
  assert.deepEqual(app.parseHash("#/work/w_1?scope=p_1%2Cp_2&q=a+b"),
    { path: ["work", "w_1"], query: { scope: "p_1,p_2", q: "a b" } });
  assert.deepEqual(app.parseHash(""), { path: [], query: {} });
  assert.equal(app.formatHash(["work", "w 1"], { scope: "p_1", q: "", x: null }), "#/work/w%201?scope=p_1");
  const h = app.formatHash(["performers", "soloists"], { with: "p_9", q: "Bö" });
  assert.deepEqual(app.parseHash(h), { path: ["performers", "soloists"], query: { with: "p_9", q: "Bö" } });
  assert.deepEqual(app.parseHash("#/x/%E0%A4%A"), { path: ["x", "%E0%A4%A"], query: {} });
});

test("the login code is taken and stripped from the URL, only with this browser's state nonce", () => {
  const t = app.takeCode("http://ma:8095/classical/?state=n1&code=abc.def&x=1#/work/w_1", "n1");
  assert.equal(t.code, "abc.def");
  assert.equal(t.url, "http://ma:8095/classical/?x=1#/work/w_1");
  const u = app.takeCode("http://ma:8095/classical/?state=n1&code=jwt", "n1");
  assert.equal(u.url, "http://ma:8095/classical/");
  assert.equal(app.takeCode("http://ma:8095/classical/#/").code, null);
  // a crafted link (no nonce, or a different one) is stripped but never stored
  for (const [href, nonce] of [["http://ma/classical/?code=evil", "n1"], ["http://ma/classical/?state=x&code=evil", "n1"],
    ["http://ma/classical/?state=&code=evil", ""], ["http://ma/classical/?state=n1&code=evil", null]]) {
    const r = app.takeCode(href, nonce);
    assert.equal(r.code, null, href);
    assert.equal(r.url, "http://ma/classical/");
  }
  assert.equal(app.loginReturnUrl("http://ma:8095", "/classical/", "ab12"), "http://ma:8095/classical/?state=ab12");
});

test("durations, life dates and year labels", () => {
  assert.equal(app.fmtDuration(0), "0:00");
  assert.equal(app.fmtDuration(65), "1:05");
  assert.equal(app.fmtDuration(3725), "1:02:05");
  assert.equal(app.lifeDates("1770-12-17", "1827"), "1770–1827");
  assert.equal(app.lifeDates("1950", null), "b. 1950");
  assert.equal(app.lifeDates(null, null), "");
  assert.equal(app.yearText({ year: 1963, year_kind: "recorded" }), "1963");
  assert.equal(app.yearText({ year: 2010, year_kind: "released" }), "rel. 2010");
  assert.equal(app.yearText({ year: null }), "");
});

test("focus filters: people, decade, text, performer scope with SHOW ALL", () => {
  const kar = { id: "p_kar", name: "Herbert von Karajan", role: "conductor", group: "conductors" };
  const bph = { id: "p_bph", name: "Berliner Philharmoniker", role: "orchestra", group: "orchestras" };
  const bohm = { id: "p_bohm", name: "Karl Böhm", role: "conductor", group: "conductors" };
  const recs = [
    rec("r1", { year: 1963, kind: "recorded", credits: [kar, bph] }),
    rec("r2", { year: 1977, kind: "recorded", credits: [kar, bph] }),
    rec("r3", { year: 2010, kind: "released", rel: 2010, credits: [bohm, bph], album: "Live in Wien" }),
  ];
  const ids = (f) => app.filterRecordings(recs, f).map((r) => r.id);
  assert.deepEqual(ids({}), ["r1", "r2", "r3"]);
  assert.deepEqual(ids({ people: { conductors: "p_kar" } }), ["r1", "r2"]);
  assert.deepEqual(ids({ people: { conductors: "p_bohm", orchestras: "p_bph" } }), ["r3"]);
  assert.deepEqual(ids({ decade: 1960 }), ["r1"]);
  assert.deepEqual(ids({ decade: 2010 }), [], "a release year is not a recording decade");
  assert.deepEqual(ids({ rel_decade: 2010 }), ["r3"]);
  assert.deepEqual(ids({ q: "bohm wien" }), ["r3"], "accent-insensitive, every word");
  assert.deepEqual(ids({ scope: ["p_kar"] }), ["r1", "r2"]);
  assert.deepEqual(ids({ scope: ["p_kar"], all: true }), ["r1", "r2", "r3"]);
  assert.deepEqual(ids({ scope: ["p_kar", "p_bph"] }), ["r1", "r2"]);
});

test("sorting is stable and puts unknowns last", () => {
  const recs = [rec("a", { year: 1990, dur: 100, who: "Zed" }), rec("b", { dur: 300, who: "Abbado" }),
    rec("c", { year: 1960, dur: 200, who: "Muti" })];
  const ids = (k) => app.sortRecordings(recs, k).map((r) => r.id);
  assert.deepEqual(ids(""), ["a", "b", "c"]);
  assert.deepEqual(ids("year"), ["c", "a", "b"]);
  assert.deepEqual(ids("year_desc"), ["a", "c", "b"]);
  assert.deepEqual(ids("performer"), ["b", "c", "a"]);
  assert.deepEqual(ids("duration"), ["b", "c", "a"]);
});

test("credits group by bucket, soloists show their instrument", () => {
  const g = app.creditGroups([
    { id: "s", name: "Anne-Sophie Mutter", role: "violin", instrument: "violin", group: "soloists" },
    { id: "k", name: "Karajan", role: "conductor", group: "conductors" },
    { id: "k", name: "Karajan", role: "conductor", group: "conductors" },
    { id: "x", name: "Someone", role: "performer", group: "other" },
  ]);
  assert.deepEqual(g.map((x) => x.group), ["conductors", "soloists", "other"]);
  assert.deepEqual(g[0].people, [{ id: "k", name: "Karajan", role: "" }]);
  assert.equal(g[1].people[0].role, "violin");
  assert.equal(g[2].people[0].role, "");
});

test("A-Z groups follow the server's letters", () => {
  const g = app.byLetter([{ letter: "B", name: "Bach" }, { letter: "B", name: "Barber" }, { letter: "M" }]);
  assert.deepEqual(g.map((x) => [x.letter, x.items.length]), [["B", 2], ["M", 1]]);
});

test("Play whole work sends the folder with shuffle off; play-from and queue options send track URIs", () => {
  const movements = [1, 2, 3].map((n) => ({ track_uri: `library://track/${n}` }));
  const r = rec("r1", { movements });
  assert.deepEqual(app.playArgs("q1", r), { queue_id: "q1", media: r.play_uri, option: "replace", shuffle: false });
  assert.deepEqual(app.playArgs("q1", r, 1),
    { queue_id: "q1", media: ["library://track/2", "library://track/3"], option: "replace", shuffle: false });
  const add = app.playArgs("q1", r, 0, "add");
  assert.deepEqual(add.media, movements.map((m) => m.track_uri));
  assert.equal("shuffle" in add, false, "MA ignores shuffle for add/next");
  assert.equal(app.playArgs("q1", rec("r2"), 0, "next").media, "classical://folder/works/w/r2");
});

test("player preselection", () => {
  const ps = [{ player_id: "a" }, { player_id: "b", available: false }];
  assert.equal(app.choosePlayer(ps, null), "a", "the only available player");
  assert.equal(app.choosePlayer(ps, "b"), "a", "a saved player that went away is not kept");
  assert.equal(app.choosePlayer([{ player_id: "a" }, { player_id: "c" }], "c"), "c");
  assert.equal(app.choosePlayer([{ player_id: "a" }, { player_id: "c" }], null), null);
});

test("errors and image URLs", () => {
  assert.match(app.errorText(403, "This command requires the library_manage scope"), /can't do this/);
  assert.match(app.errorText(404, ""), /Not found/);
  assert.match(app.errorText(400, "Not found"), /hidden from your account/);
  assert.equal(app.errorText(400, "role must be all"), "role must be all");
  assert.equal(app.errorText(500, "boom"), "boom");
  assert.equal(app.imageSrc("imageproxy/abc123?size=256"), "../imageproxy/abc123?size=256");
  assert.equal(app.imageSrc("javascript:alert(1)"), null);
  assert.equal(app.imageSrc("https://evil/x.png"), null);
  assert.equal(app.imageSrc(null), null);
});

test("the script never parses data as HTML", () => {
  const src = readFileSync(APP, "utf8");
  assert.doesNotMatch(src, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/);
});

test("credits show the character or context of a nested credit", () => {
  const g = app.creditGroups([
    { id: "a", name: "Ain Anger", role: "bass", group: "soloists", detail: "Pater Profundus" },
    { id: "p", name: "Antonio Pappano", role: "conductor", group: "conductors", detail: "Brahms concert" },
  ]);
  assert.equal(g.find((x) => x.group === "soloists").people[0].role, "bass · Pater Profundus");
  assert.equal(g.find((x) => x.group === "conductors").people[0].role, "Brahms concert");
});

test("review cards never repeat their title as the subtitle", () => {
  assert.equal(app.reviewSubtitle({ title: "Beethoven: Fidelio", subtitle: "several releases match" }),
    "several releases match");
  assert.equal(app.reviewSubtitle({ title: "Beethoven: Fidelio", album: "Beethoven: Fidelio" }), "");
  assert.equal(app.reviewSubtitle({ title: "Obertura", album: "FIDELIO" }), "FIDELIO");
});

test("Most acclaimed sorts by acclaim, then oldest first; unacclaimed keep their order last", () => {
  const recs = [rec("a", { year: 1990 }), rec("b", { year: 1970 }), rec("c", { year: 1980 }), rec("d", { year: 1960 })];
  recs[1].acclaim = 1.3; recs[2].acclaim = 2.5; recs[3].acclaim = 1.3;
  assert.deepEqual(app.sortRecordings(recs, "acclaim").map((r) => r.id), ["c", "d", "b", "a"]);
  assert.equal(app.anyAcclaim(recs), true);
  assert.equal(app.anyAcclaim([rec("x"), { ...rec("y"), acclaim: 1 }]), false);
});

test("badge text names the category and, on request, how the award applies", () => {
  const a = { text: "Grammy nominee 1963", category: "Best Opera Recording", note: null };
  assert.equal(app.badgeText(a), "Grammy nominee 1963 · Best Opera Recording");
  const b = { text: "Gramophone Award 2012", category: "Opera", note: "awarded to another release of this recording (2012)" };
  assert.equal(app.badgeText(b, true), "Gramophone Award 2012 · Opera (awarded to another release of this recording (2012))");
  assert.equal(app.badgeText({ text: "Wikidata award", note: "for the album" }, true), "Wikidata award (for the album)");
});

test("outbound links are https only", () => {
  assert.equal(app.safeHref("https://www.discogs.com/release/123"), "https://www.discogs.com/release/123");
  assert.equal(app.safeHref("http://www.allmusic.com/album/x"), null);
  assert.equal(app.safeHref("javascript:alert(1)"), null);
  assert.equal(app.safeHref("data:text/html,x"), null);
  assert.equal(app.safeHref("https://x.org/\" onmouseover=1"), null);
  assert.equal(app.safeHref(null), null);
});

test("Today picks and the day line read cleanly", () => {
  assert.deepEqual(app.pickLines({ title: "Cello Concerto", composer: "Edward Elgar", recording_label: "Ma · LSO",
    reason: "Premiered on this day in 1919" }),
  { title: "Cello Concerto", sub: "Edward Elgar · Ma · LSO", why: "Premiered on this day in 1919" });
  assert.deepEqual(app.pickLines({ title: "Messiah", composer: "Handel", recording_label: "Messiah" }),
    { title: "Messiah", sub: "Handel", why: "" });
  assert.deepEqual(app.pickLines(null), { title: "", sub: "", why: "" });
  assert.equal(app.daySubtitle({ liturgical: "Christmas Day", season: "winter" }), "Christmas Day · Winter");
  assert.equal(app.daySubtitle({ liturgical: null, season: "autumn" }), "Autumn");
  assert.equal(app.daySubtitle({}), "");
});

test("Today asks for the browser's local day, not the UTC one", () => {
  assert.equal(app.localDate(new Date(2026, 9, 27, 23, 59)), "2026-10-27");
  assert.equal(app.localDate(new Date(2027, 0, 5, 0, 1)), "2027-01-05");
  assert.match(app.localDate(), /^\d{4}-\d{2}-\d{2}$/);
});

test("sign-in probes stored tokens first outside ingress; the credential-less probe runs last", () => {
  assert.deepEqual(app.probeOrder("/classical/", "t1", "t2"), ["own", "borrowed", "ingress"]);
  assert.deepEqual(app.probeOrder("/classical/", null, null), ["ingress"]);
  assert.deepEqual(app.probeOrder("/api/hassio_ingress/abc/classical/", "t1", null), ["ingress", "own"]);
});

test("provider badges: streaming services first, local only beside a streaming copy", () => {
  const q = { instance: "qobuz--a", name: "Qobuz", streaming: true };
  const q2 = { instance: "qobuz--b", name: "Qobuz", streaming: true };
  const loc = { instance: "filesystem_smb--x", name: "Network share", streaming: false };
  assert.deepEqual(app.sourceLabels([loc, q, q2], true).map((s) => s.text), ["Qobuz", "Network share"]);
  assert.deepEqual(app.sourceLabels([loc, q], false).map((s) => s.text), ["Qobuz"]);
  assert.deepEqual(app.sourceLabels([loc], false), []);
  assert.deepEqual(app.sourceLabels(undefined, true), []);
  assert.equal(app.anyStreaming([{ sources: [loc] }, { sources: [q] }]), true);
  assert.equal(app.anyStreaming([{ sources: [loc] }, {}]), false);
});

test("work dates: row date, dates line with sources, works query defaults drop out", () => {
  assert.equal(app.rowDate({ composed_text: "1804–05", premiered: 1805 }), "1804–05");
  assert.equal(app.rowDate({ composed_text: null, premiered: 1805 }), "premiered 1805");
  assert.equal(app.rowDate({}), "");
  assert.equal(app.datesLine(null), null);
  assert.equal(app.datesLine({ text: null }), null);
  assert.deepEqual(app.datesLine({ text: "composed 1804–05 · premiered 1805", sources: ["MusicBrainz", "premieres list"],
    premiered: { place: "Theater an der Wien" } }),
    { text: "composed 1804–05 · premiered 1805 (Theater an der Wien)", source: "from MusicBrainz and premieres list" });
  assert.deepEqual(app.datesLine({ text: "composed 1830", sources: ["file tags"], premiered: null }),
    { text: "composed 1830", source: "from file tags" });
  assert.deepEqual(app.worksQuery({ composer: "c1", dk: "premiered", sort: "date" }, { dk: "composed", period: "romantic" }),
    { composer: "c1", period: "romantic", sort: "date" });
  assert.deepEqual(app.worksQuery({ sort: "date", decade: "1860" }, { sort: "catalogue", decade: "" }), {});
});

test("MusicBrainz work candidates link only to a real work id on musicbrainz.org", () => {
  const c = app.mbWorkCandidates({ detail: { candidates: [
    { mbid: "C35B4956-0000-4000-8000-000000000001", title: "Symphony no. 9", why: "same catalogue number (Op. 125)" },
    { mbid: "javascript:alert(1)", title: "x" },
    { title: "no id" },
  ] } });
  assert.equal(c.length, 2);
  assert.equal(c[0].href, "https://musicbrainz.org/work/c35b4956-0000-4000-8000-000000000001");
  assert.equal(c[0].why, "same catalogue number (Op. 125)");
  assert.equal(c[1].href, null);
  assert.deepEqual(app.mbWorkCandidates({}), []);
});

test("a work page's MusicBrainz link line: only a well-formed MBID makes a link", () => {
  const id = "00000005-0000-4000-8000-00000000ABCD";
  assert.deepEqual(app.mbLinkLine({ mbid: id, auto: true, title: "Concerto no. 4" }),
    { mbid: id.toLowerCase(), text: "Linked automatically to Concerto no. 4:",
      href: "https://musicbrainz.org/work/" + id.toLowerCase() });
  assert.equal(app.mbLinkLine({ mbid: id, auto: false }).text, "Linked to a MusicBrainz work:");
  for (const bad of [null, {}, { mbid: "javascript:alert(1)" }, { mbid: id + "/../x" }, { mbid: 5 }])
    assert.equal(app.mbLinkLine(bad), null);
});

test("review filters: defaults dropped, a new kind clears the reason, args carry the paging", () => {
  assert.deepEqual(app.reviewQuery({ kind: "info", reason: "x", q: "bach" }, { kind: "mb" }), { kind: "mb", q: "bach" });
  assert.deepEqual(app.reviewQuery({ kind: "mb", reason: "x" }, { kind: "mb" }), { kind: "mb", reason: "x" });
  assert.deepEqual(app.reviewQuery({ status: "dismissed", sort: "acclaim" }, { status: "open", sort: "impact" }), {});
  assert.deepEqual(app.reviewQuery({ junk: 1, album: "12" }, { q: "" }), { album: "12" });
  assert.deepEqual(app.reviewArgs({ kind: "name", composer: "c_1" }, 30),
    { sort: "impact", offset: 30, limit: 30, kind: "name", composer: "c_1" });
  assert.deepEqual(app.reviewArgs({}), { sort: "impact", offset: 0, limit: 30 });
});

test("review cards: meta line, action counts and the recent-decisions line", () => {
  assert.equal(app.cardMeta({ n: 14, impact: 208, acclaim: 0 }), "14 items · 208 tracks");
  assert.equal(app.cardMeta({ n: 1, impact: 1, acclaim: 2.25 }), "1 item · 1 track · acclaim 2.3");
  assert.equal(app.actionCount([{ op: "dismiss", items: ["a", "b", "c"] }]), 3);
  assert.equal(app.actionCount([{ op: "work_mb_link" }, { op: "work_mb_link" }]), 2);
  assert.equal(app.actionCount([{ op: "restore", keys: ["a"] }]), 1);
  const now = 1_000_000;
  assert.equal(app.historyLine({ at: now - 30, actions: 1 }, now), "just now");
  assert.equal(app.historyLine({ at: now - 600, actions: 3, undone: true }, now), "10 min ago · 3 changes · undone");
  assert.equal(app.historyLine({ at: now - 7200, actions: 1, blocked_by: 9 }, now), "2 h ago · undo decision #9 first");
  assert.equal(app.historyLine({ at: now - 86400 * 2, actions: 1 }, now), "2 days ago");
});

test("a credit card lists each album once, with every composer the credit could be", () => {
  const it = (id, album, other) => ({ id, album_id: album, album: "A" + album, detail: { other } });
  const rows = app.albumRows([it("1", "x", "Robert Schumann"), it("2", "x", "Clara Schumann"), it("3", "y", "Robert Schumann")]);
  assert.deepEqual(rows.map((r) => [r.title, r.n, r.others]),
    [["Ax", 2, ["Robert Schumann", "Clara Schumann"]], ["Ay", 1, ["Robert Schumann"]]]);
});

test("a confirm gate: a fast double click only arms it, a second tap later runs once, never twice at a time", () => {
  const g = app.tapGate(true, 400, 5000);
  assert.equal(g.tap(1000), "arm");
  assert.equal(g.tap(1050), "wait");            // the second click of a double click
  assert.equal(g.tap(1600), "run");
  assert.equal(g.tap(1601), "busy");            // in flight: no second request
  assert.equal(g.tap(2500), "busy");
  g.done();
  assert.equal(g.tap(3000), "arm");             // confirmed once: the next run asks again
  assert.equal(g.tap(9000), "arm");             // too late: armed afresh
  g.disarm();
  assert.equal(g.tap(9100), "arm");
  const one = app.tapGate(false);
  assert.equal(one.tap(0), "run");
  assert.equal(one.tap(1), "busy");
  one.done();
  assert.equal(one.tap(2), "run");
  assert.equal(app.actionCount([{ op: "restore", keys: ["a", "b"] }]), 2);
  assert.equal(app.historyLine({ at: 0, actions: 1, blocked_by: 9, blocked_by_other: true }, 10),
    "just now · changed since by someone else");
});

test("box sets: where a recording sits, a box's summary and disc headings", () => {
  assert.equal(app.boxLine({ id: "x1", title: "Beethoven: Complete Works", disc: 61, single: false }),
    "from Beethoven: Complete Works, CD 61");
  assert.equal(app.boxLine({ id: "x1", title: "Karajan Edition", disc: null }), "from Karajan Edition");
  assert.equal(app.boxLine(null), null);
  assert.equal(app.boxSummary({ discs: 6, works: 2, composers: ["Ludwig van Beethoven"] }),
    "6 discs · 2 works · Ludwig van Beethoven");
  assert.equal(app.boxSummary({ discs: 12, total: 155, works: 30, composers: ["A", "B", "C"] }),
    "12 discs of 155 · 30 works · 3 composers");
  assert.equal(app.boxSummary({ discs: 1, works: 1, composers: [] }), "1 disc · 1 work");
  assert.equal(app.discTitle({ disc: 61, subtitle: "Leonore Part 1" }), "CD 61 · Leonore Part 1");
  assert.equal(app.discTitle({ disc: 3, subtitle: null }), "CD 3");
});

test("an album's subtitle names the other composers on a compilation", () => {
  const cs = [{ id: "c_b", name: "Bach" }, { id: "c_m", name: "Mozart" }, { id: "c_r", name: "Rimsky-Korsakov" }];
  assert.equal(app.albumSubtitle({ year: 1999, works: 1, composers: cs }, "c_m"), "1999 · 1 work · with Bach, Rimsky-Korsakov");
  assert.equal(app.albumSubtitle({ year: null, works: 2, composers: [cs[1]] }, "c_m"), "2 works");
  assert.equal(app.albumSubtitle({ works: 3, composers: cs }, null), "3 works · Bach, Mozart, Rimsky-Korsakov");
  assert.equal(app.albumSubtitle({ works: 5, composers: cs.concat(cs.map((c) => ({ id: c.id + "2", name: c.name }))) }, "c_m"),
    "5 works · 6 composers");
  // no composer of its own (the title and year lists): every composer counts
  assert.equal(app.albumSubtitle({ works: 4, composers: cs.concat([{ id: "c_h", name: "Haydn" }]) }, null), "4 works · 4 composers");
});

test("liner notes: bio lines, fact rows, the guide's heading, paragraphs and thumbnails", () => {
  assert.equal(app.bioLine({ group: "conductors", role: "conductor",
    bio: { born: 1908, died: 1989, description: "Austrian conductor" } }), "Conductor · 1908–1989 · Austrian conductor");
  assert.equal(app.bioLine({ group: "soloists", role: "violin", bio: { born: 1963 } }), "Violin · b. 1963");
  assert.equal(app.bioLine({ group: "orchestras", role: "orchestra", bio: { description: "orchestra" } }), "Orchestra",
    "a description that only repeats the role is dropped");
  assert.equal(app.bioLine({ bio: { born: 1908, died: 1989 } }), "1908–1989", "the performer page: no role");
  assert.deepEqual(app.noteFacts({ key: "C minor", catalogue: "Op. 67",
    dates: { composed: { text: "1804–08" }, premiered: { text: "22 December 1808", place: "Vienna" } } }),
  [["Key", "C minor"], ["Catalogue", "Op. 67"], ["Composed", "1804–08"], ["Premiered", "22 December 1808, Vienna"]]);
  assert.deepEqual(app.noteFacts({ key: null, dates: { composed: null, premiered: null } }), []);
  assert.deepEqual(app.noteFacts(null), []);
  assert.equal(app.guideTitle({ model: "claude-opus-5-5" }), "AI-written listening guide (claude-opus-5-5)");
  assert.equal(app.guideTitle({}), "AI-written listening guide");
  assert.deepEqual(app.paragraphs("One.\n\n  Two.\n \nThree.\n"), ["One.", "Two.", "Three."]);
  assert.deepEqual(app.paragraphs(null), []);
  assert.equal(app.thumbSrc("https://upload.wikimedia.org/wikipedia/commons/a.jpg"),
    "https://upload.wikimedia.org/wikipedia/commons/a.jpg");
  for (const bad of ["http://upload.wikimedia.org/a.jpg", "https://upload.wikimedia.org.evil.example/a.jpg",
    "https://example.org/a.jpg", "javascript:alert(1)", null, 7]) assert.equal(app.thumbSrc(bad), null);
});
