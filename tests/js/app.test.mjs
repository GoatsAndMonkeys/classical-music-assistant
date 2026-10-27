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
