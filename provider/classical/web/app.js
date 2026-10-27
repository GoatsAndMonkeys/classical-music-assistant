/* Classical companion UI (PLAN.md §6, M3). Vanilla ES2019, one file, no build step.
 *
 * Every byte of data comes from Music Assistant's authenticated API (`POST ../api`); this file only renders it.
 * Untrusted text (tags, names) is only ever set with textContent / attributes, never parsed as HTML.
 * The pure helpers at the top are exported for `node --test` (tests/js/app.test.mjs); the DOM code below
 * runs only in a browser.
 */
"use strict";

(function () {
  // ---- pure helpers ---------------------------------------------------------------------------------------

  var BUCKETS = ["conductors", "orchestras", "chamber", "choirs", "soloists", "other"];
  var BUCKET_TITLES = {
    conductors: "Conductors", orchestras: "Orchestras", chamber: "Chamber", choirs: "Choirs",
    soloists: "Soloists", other: "Other performers",
  };
  var ROLE_TITLES = { all: "All" };
  BUCKETS.forEach(function (b) { ROLE_TITLES[b] = b === "other" ? "Other" : BUCKET_TITLES[b]; });
  var PAGE = 100;
  var IMAGE_RE = /^imageproxy\/[0-9A-Za-z_-]+\?size=\d+$/;

  /** "#/work/w_1?scope=p_2" -> {path: ["work", "w_1"], query: {scope: "p_2"}} */
  function parseHash(hash) {
    var h = String(hash || "").replace(/^#/, "").replace(/^\//, "");
    var qi = h.indexOf("?");
    var path = (qi >= 0 ? h.slice(0, qi) : h).split("/").filter(Boolean).map(safeDecode);
    var query = {};
    if (qi >= 0) {
      h.slice(qi + 1).split("&").forEach(function (kv) {
        if (!kv) return;
        var i = kv.indexOf("=");
        var k = safeDecode(i >= 0 ? kv.slice(0, i) : kv);
        query[k] = safeDecode(i >= 0 ? kv.slice(i + 1).replace(/\+/g, " ") : "");
      });
    }
    return { path: path, query: query };
  }

  function safeDecode(s) {
    try { return decodeURIComponent(s); } catch (e) { return s; }
  }

  /** (["work", "w_1"], {scope: "p_2", q: ""}) -> "#/work/w_1?scope=p_2" (empty values dropped) */
  function formatHash(path, query) {
    var h = "#/" + (path || []).map(encodeURIComponent).join("/");
    var parts = [];
    Object.keys(query || {}).forEach(function (k) {
      var v = query[k];
      if (v === undefined || v === null || v === "" || v === false) return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
    });
    return parts.length ? h + "?" + parts.join("&") : h;
  }

  /**
   * The login redirect returns `?state=<nonce>&code=<JWT>`: take the code and the URL without either (to
   * replaceState at once). The code counts only when `state` matches the nonce this browser stored before it
   * left for the login page, so a crafted link can't sign the page in to someone else's account.
   */
  function takeCode(href, expected) {
    var u = new URL(href);
    var code = u.searchParams.get("code"), st = u.searchParams.get("state");
    if (!code && !st) return { code: null, url: href };
    u.searchParams.delete("code");
    u.searchParams.delete("state");
    var search = u.searchParams.toString();
    return { code: code && expected && st === expected ? code : null,
      url: u.origin + u.pathname + (search ? "?" + search : "") + u.hash };
  }

  /** MA's login page appends `code` to this URL (same origin, so MA trusts it without asking). */
  function loginReturnUrl(origin, pathname, nonce) {
    return origin + pathname + "?state=" + encodeURIComponent(nonce);
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    var ss = (s < 10 ? "0" : "") + s;
    return h ? h + ":" + (m < 10 ? "0" : "") + m + ":" + ss : m + ":" + ss;
  }

  function lifeDates(born, died) {
    var b = born ? String(born).slice(0, 4) : "", d = died ? String(died).slice(0, 4) : "";
    if (b && d) return b + "–" + d;
    if (b) return "b. " + b;
    if (d) return "d. " + d;
    return "";
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : (many || one + "s"));
  }

  /** "1963" for a recording year, "rel. 2010" when only the release year is known. */
  function yearText(rec) {
    if (!rec || !rec.year) return "";
    return rec.year_kind === "released" ? "rel. " + rec.year : String(rec.year);
  }

  /** A recording's provider badges ("Qobuz", "Local files"): one per service, streaming first. Local files
   * are named only when `withLocal` (the work also has streaming recordings), so a local-only library stays
   * as it was. */
  function sourceLabels(sources, withLocal) {
    var seen = {};
    var out = [];
    (sources || []).slice().sort(function (a, b) { return (b.streaming ? 1 : 0) - (a.streaming ? 1 : 0); })
      .forEach(function (s) {
        if (!s || !s.name || seen[s.name] || (!s.streaming && !withLocal)) return;
        seen[s.name] = true;
        out.push({ text: s.name, streaming: !!s.streaming });
      });
    return out;
  }

  function anyStreaming(recs) {
    return (recs || []).some(function (r) { return (r.sources || []).some(function (s) { return s && s.streaming; }); });
  }

  function workSubtitle(w) {
    return [w.composer, w.catalogue, w.key, w.work_type].filter(function (x, i, a) {
      return x && a.indexOf(x) === i && !(i > 0 && w.title && x !== w.work_type && w.title.indexOf(x) >= 0);
    }).join(" · ");
  }

  /** A work row's date: "1804–05", else "premiered 1805", else nothing. */
  function rowDate(w) {
    if (w.composed_text) return w.composed_text;
    return w.premiered ? "premiered " + w.premiered : "";
  }

  /** The work page's dates line and its subtle provenance ("from MusicBrainz and file tags"). */
  function datesLine(d) {
    if (!d || !d.text) return null;
    var src = (d.sources || []).filter(Boolean);
    var place = d.premiered && d.premiered.place ? " (" + d.premiered.place + ")" : "";
    return { text: d.text + place,
      source: src.length ? "from " + (src.length > 1 ? src.slice(0, -1).join(", ") + " and " + src[src.length - 1] : src[0]) : "" };
  }

  /** The works list's query after a filter change; defaults drop out of the hash. */
  function worksQuery(query, patch) {
    var q = {};
    ["composer", "type", "q", "dk", "from", "to", "performer", "period", "decade", "sort"].forEach(function (k) {
      q[k] = query[k];
    });
    Object.assign(q, patch);
    if (q.dk === "composed") q.dk = "";
    if (q.sort === "catalogue") q.sort = "";
    Object.keys(q).forEach(function (k) { if (q[k] === undefined || q[k] === null || q[k] === "") delete q[k]; });
    return q;
  }

  function words(q) {
    return String(q || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").split(/\s+/)
      .filter(Boolean);
  }

  function fold(s) {
    return String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
  }

  function recordingText(rec) {
    var parts = [rec.label, rec.who, rec.album, rec.album_artist, rec.venue, yearText(rec)];
    (rec.credits || []).forEach(function (c) { parts.push(c.name, c.instrument, c.role); });
    return fold(parts.filter(Boolean).join(" "));
  }

  /**
   * The work page's focus: `f = {q, people: {bucket: personId}, decade, rel_decade, scope: [ids], all}`.
   * A performer scope (from the performer page) applies until SHOW ALL (`all`) is set (OzGav).
   */
  function recordingMatches(rec, f) {
    f = f || {};
    var ids = (rec.credits || []).map(function (c) { return c.id; });
    if (f.scope && f.scope.length && !f.all) {
      for (var i = 0; i < f.scope.length; i++) if (ids.indexOf(f.scope[i]) < 0) return false;
    }
    var people = f.people || {};
    for (var b in people) {
      if (people[b] && ids.indexOf(people[b]) < 0) return false;
    }
    if (f.decade && !(rec.year_kind === "recorded" && rec.decade === Number(f.decade))) return false;
    if (f.rel_decade && !(rec.release_year && Math.floor(rec.release_year / 10) * 10 === Number(f.rel_decade))) {
      return false;
    }
    var ws = words(f.q);
    if (ws.length) {
      var text = rec._text || recordingText(rec);
      for (var j = 0; j < ws.length; j++) if (text.indexOf(ws[j]) < 0) return false;
    }
    return true;
  }

  function filterRecordings(recs, f) {
    return (recs || []).filter(function (r) { return recordingMatches(r, f); });
  }

  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  /** Sort keys: "" (the server's order), year, year_desc, performer, duration. Stable; unknowns last. */
  function sortRecordings(recs, key) {
    var out = (recs || []).map(function (r, i) { return [r, i]; });
    var by = {
      year: function (a, b) { return cmp(a.year || 9999, b.year || 9999); },
      year_desc: function (a, b) { return cmp(b.year || 0, a.year || 0); },
      performer: function (a, b) { return cmp(fold(a.who || "￿"), fold(b.who || "￿")); },
      duration: function (a, b) { return cmp(b.duration || 0, a.duration || 0); },
      acclaim: function (a, b) { return cmp(b.acclaim || 1, a.acclaim || 1) || cmp(a.year || 9999, b.year || 9999); },
    }[key];
    out.sort(function (x, y) { return (by ? by(x[0], y[0]) : 0) || x[1] - y[1]; });
    return out.map(function (x) { return x[0]; });
  }

  /** A badge's words: "Grammy nominee 1963 · Best Opera Recording", with how it applies when not plainly to
   * this recording ("for the album", "awarded to another release of this recording (1963)"). */
  function badgeText(a, withNote) {
    var t = a.text + (a.category ? " · " + a.category : "");
    return withNote && a.note ? t + " (" + a.note + ")" : t;
  }

  /** Whether any recording carries a counted accolade (the "Most acclaimed" sort is offered only then). */
  function anyAcclaim(recs) {
    return (recs || []).some(function (r) { return (r.acclaim || 1) > 1; });
  }

  /** A review card's second line: never a repeat of its title (an album item's title is the album). */
  function reviewSubtitle(it) {
    var sub = it.subtitle !== undefined ? it.subtitle : (it.album || "");
    return sub && sub !== it.title ? sub : "";
  }

  var REVIEW_KEYS = ["kind", "reason", "composer", "album", "q", "status", "sort"];

  /** The review page's hash query after a filter change; defaults (open, by impact) and empties dropped.
   * Changing the card kind clears the reason, which belongs to one kind. */
  function reviewQuery(query, patch) {
    var q = {};
    REVIEW_KEYS.forEach(function (k) { q[k] = (query || {})[k]; });
    patch = patch || {};
    if ("kind" in patch && patch.kind !== q.kind) q.reason = "";
    Object.assign(q, patch);
    if (q.status === "open") q.status = "";
    if (q.sort === "impact") q.sort = "";
    Object.keys(q).forEach(function (k) { if (q[k] === undefined || q[k] === null || q[k] === "") delete q[k]; });
    return q;
  }

  /** classical/review_triage arguments for a review hash query. */
  function reviewArgs(query, offset) {
    var a = { sort: (query && query.sort) || "impact", offset: offset || 0, limit: 30 };
    REVIEW_KEYS.forEach(function (k) { if (k !== "sort" && query && query[k]) a[k] = query[k]; });
    return a;
  }

  /** A credit card's rows: one per album (a bare credit can raise several notes on one album), each naming the
   * composers the credit might mean. */
  function albumRows(items) {
    var rows = [], at = {};
    (items || []).forEach(function (it) {
      var k = it.album_id || it.id;
      var row = at[k];
      if (!row) {
        row = at[k] = { item: it, title: it.album || it.title, others: [], n: 0 };
        rows.push(row);
      }
      row.n++;
      var o = it.detail && it.detail.other;
      if (o && row.others.indexOf(o) < 0) row.others.push(o);
    });
    return rows;
  }

  /** "14 items · 208 tracks · acclaim 3" for a decision card. */
  function cardMeta(card) {
    var out = [plural(card.n || 0, "item")];
    if (card.impact) out.push(plural(card.impact, "track"));
    if (card.acclaim) out.push("acclaim " + Math.round(card.acclaim * 10) / 10);
    return out.join(" · ");
  }

  /** How many review items an action list touches (a hide lists its items, a restore its keys; other actions
   * are one each). */
  function actionCount(actions) {
    return (actions || []).reduce(function (n, a) {
      return n + (a.op === "dismiss" ? (a.items || []).length : a.op === "restore" ? (a.keys || []).length : 1);
    }, 0);
  }

  /** A tap gate for buttons that change things. With `confirm`, the first tap arms it and a second tap at least
   * `gap` ms later (and within `win` ms) runs it, so a fast double click can't confirm by itself. Either way
   * nothing runs again until `done()`: one request at a time. tap(now) answers "arm", "wait", "busy" or "run". */
  function tapGate(confirm, gap, win) {
    var armedAt = null, busy = false;
    gap = gap == null ? 400 : gap;
    win = win == null ? 5000 : win;
    return {
      tap: function (now) {
        if (busy) return "busy";
        if (confirm && (armedAt === null || now - armedAt > win)) { armedAt = now; return "arm"; }
        if (confirm && now - armedAt < gap) return "wait";
        armedAt = null;
        busy = true;
        return "run";
      },
      done: function () { busy = false; },
      disarm: function () { armedAt = null; },
    };
  }

  function agoText(at, now) {
    var s = Math.max(0, Math.round((now || Date.now() / 1000) - at));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return plural(Math.floor(s / 86400), "day") + " ago";
  }

  /** A recent decision's second line: when, how many changes, and why it can't be undone now. */
  function historyLine(b, now) {
    var out = [agoText(b.at, now)];
    if (b.actions > 1) out.push(plural(b.actions, "change"));
    if (b.undone) out.push("undone");
    else if (b.blocked_by_other) out.push("changed since by someone else");
    else if (b.blocked_by) out.push("undo decision #" + b.blocked_by + " first");
    return out.join(" · ");
  }

  /** Credits grouped for display: [{group, title, people: [{id, name, role}]}] in bucket order. */
  function creditGroups(credits) {
    var groups = {};
    (credits || []).forEach(function (c) {
      var g = c.group || "other";
      var list = groups[g] || (groups[g] = []);
      var role = g === "soloists" ? (c.instrument || c.role) : (g === "other" ? c.role : "");
      // the character sung or the context of the credit ("bass · Pater Profundus")
      role = [role && role !== "performer" ? role : "", c.detail || ""].filter(Boolean).join(" · ");
      if (!list.some(function (p) { return p.id === c.id && p.role === role; })) {
        list.push({ id: c.id, name: c.name, role: role });
      }
    });
    return BUCKETS.filter(function (b) { return groups[b]; }).map(function (b) {
      return { group: b, title: BUCKET_TITLES[b], people: groups[b] };
    });
  }

  /** Items already sorted by the server, grouped by their `letter`. */
  function byLetter(items) {
    var out = [];
    (items || []).forEach(function (it) {
      var l = it.letter || "#";
      if (!out.length || out[out.length - 1].letter !== l) out.push({ letter: l, items: [] });
      out[out.length - 1].items.push(it);
    });
    return out;
  }

  /**
   * `player_queues/play_media` arguments. Playing a whole recording sends its folder (movement order) with
   * `shuffle: false`: a folder isn't an ordered media type, so MA would otherwise keep the queue's shuffle.
   * From movement k it sends the remaining track URIs. add/next ignore `shuffle` in MA, so none is sent.
   */
  function playArgs(queueId, rec, from, option) {
    option = option || "replace";
    var movs = (rec && rec.movements) || [];
    var media;
    if (option === "replace" && !from) media = rec.play_uri;
    else if (movs.length) media = movs.slice(from || 0).map(function (m) { return m.track_uri; });
    else media = rec.play_uri;
    var args = { queue_id: queueId, media: media, option: option };
    if (option === "replace" || option === "play") args.shuffle = false;
    return args;
  }

  /** Which player to preselect: the saved one if still there, else the only one. */
  function choosePlayer(players, saved) {
    var ok = (players || []).filter(function (p) { return p.available !== false && p.enabled !== false; });
    if (saved && ok.some(function (p) { return p.player_id === saved; })) return saved;
    return ok.length === 1 ? ok[0].player_id : null;
  }

  function playerName(p) {
    return (p && (p.display_name || p.name || p.player_id)) || "";
  }

  function errorText(status, text) {
    text = String(text || "").trim();
    if (status === 401) return "Please sign in again.";
    if (status === 403) return "Your account can't do this" + (text ? " (" + text + ")" : "") + ".";
    if (status === 404 || (status === 400 && text === "Not found")) return "Not found — it may be hidden from your account or no longer in the library.";
    return text || "Something went wrong (" + status + ").";
  }

  function imageSrc(url) {
    return url && IMAGE_RE.test(url) ? "../" + url : null;
  }

  /** An outbound link's href, or null: only absolute https URLs (never javascript:, data: or plain http). */
  function safeHref(url) {
    return typeof url === "string" && /^https:\/\/[^\s"'<>\\]+$/i.test(url) ? url : null;
  }

  /** A "Link to MusicBrainz work?" item's candidates: the MusicBrainz title, why it matched, and its page. */
  function mbWorkCandidates(it) {
    var c = (it && it.detail && it.detail.candidates) || [];
    return c.filter(function (x) { return x && typeof x.mbid === "string"; }).map(function (x) {
      var ok = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(x.mbid);
      return { title: String(x.title || x.mbid), why: String(x.why || ""), taken: !!x.taken_by,
        href: ok ? "https://musicbrainz.org/work/" + x.mbid.toLowerCase() : null };
    });
  }

  /** A work page's MusicBrainz link (one the linking job made, or a person accepted): its text and page. */
  function mbLinkLine(link) {
    if (!link || typeof link.mbid !== "string" ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(link.mbid)) return null;
    return { mbid: link.mbid.toLowerCase(),
      text: (link.auto ? "Linked automatically to " : "Linked to ") + String(link.title || "a MusicBrainz work") + ":",
      href: "https://musicbrainz.org/work/" + link.mbid.toLowerCase() };
  }

  /** A Today pick's lines: title, "composer · recording", and why it was chosen (never repeating the title). */
  function pickLines(p) {
    p = p || {};
    var sub = [p.composer, p.recording_label && p.recording_label !== p.title ? p.recording_label : ""]
      .filter(Boolean).join(" · ");
    return { title: p.title || "", sub: sub, why: p.reason || "" };
  }

  /** The Today page's subtitle: "Sunday after Trinity 21 · Autumn" (season capitalised; empties dropped). */
  function daySubtitle(d) {
    d = d || {};
    var season = d.season ? String(d.season).charAt(0).toUpperCase() + String(d.season).slice(1) : "";
    return [d.liturgical, season].filter(Boolean).join(" · ");
  }

  /** The browser's own calendar day as YYYY-MM-DD (local time, not UTC: "today" is the listener's day). */
  function localDate(d) {
    d = d || new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  /** Which credentials to try, in order. Under HA ingress the proxy's own session comes first; elsewhere a
   * credential-less probe always fails (one expected 401), so it only runs when no token is stored. */
  function probeOrder(pathname, own, borrowed) {
    var ingress = /\/hassio_ingress\//.test(pathname || "");
    var out = [];
    if (ingress) out.push("ingress");
    if (own) out.push("own");
    if (borrowed) out.push("borrowed");
    if (!ingress) out.push("ingress");
    return out;
  }

  /** "from Beethoven: Complete Works, CD 61": where a recording sits in a box set. */
  function boxLine(box) {
    if (!box || !box.title) return null;
    return "from " + box.title + (box.disc != null ? ", CD " + box.disc : "");
  }

  /** "6 discs · 2 works · Beethoven" under a box set's name. */
  function boxSummary(b) {
    if (!b) return "";
    var who = (b.composers || []).length > 2 ? plural(b.composers.length, "composer") : (b.composers || []).join(", ");
    return [b.discs ? plural(b.discs, "disc") + (b.total && b.total !== b.discs ? " of " + b.total : "") : null,
      b.works ? plural(b.works, "work") : null, who || null].filter(Boolean).join(" · ");
  }

  /** A box disc's heading: "CD 61 · Leonore Part 1". */
  function discTitle(d) {
    var n = d.disc != null ? "CD " + d.disc : "Disc";
    return d.subtitle ? n + " · " + d.subtitle : n;
  }

  /** "1999 · 2 works · with Bach, Rimsky-Korsakov" under an album: the other composers on a compilation. */
  function albumSubtitle(a, cid) {
    var others = (a.composers || []).filter(function (c) { return c.id !== cid; }).map(function (c) { return c.name; });
    var who = others.length > 3 ? plural((a.composers || []).length, "composer") : others.length ? (cid ? "with " : "") + others.join(", ") : null;
    return [a.year, a.works ? plural(a.works, "work") : null, who].filter(Boolean).join(" · ");
  }

  var pure = {
    BUCKETS: BUCKETS, parseHash: parseHash, formatHash: formatHash, takeCode: takeCode, fmtDuration: fmtDuration,
    lifeDates: lifeDates, yearText: yearText, sourceLabels: sourceLabels, anyStreaming: anyStreaming,
    workSubtitle: workSubtitle, rowDate: rowDate, datesLine: datesLine, worksQuery: worksQuery,
    recordingMatches: recordingMatches,
    filterRecordings: filterRecordings, sortRecordings: sortRecordings, creditGroups: creditGroups,
    reviewSubtitle: reviewSubtitle, albumRows: albumRows, reviewQuery: reviewQuery, reviewArgs: reviewArgs, cardMeta: cardMeta,
    actionCount: actionCount, tapGate: tapGate, historyLine: historyLine, mbWorkCandidates: mbWorkCandidates, mbLinkLine: mbLinkLine, badgeText: badgeText, anyAcclaim: anyAcclaim, safeHref: safeHref,
    byLetter: byLetter, playArgs: playArgs, loginReturnUrl: loginReturnUrl, choosePlayer: choosePlayer, errorText: errorText, imageSrc: imageSrc,
    plural: plural, pickLines: pickLines, boxLine: boxLine, boxSummary: boxSummary, albumSubtitle: albumSubtitle, discTitle: discTitle, daySubtitle: daySubtitle, localDate: localDate, probeOrder: probeOrder,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = pure;
  if (typeof document === "undefined") return;

  // ---- DOM helpers ----------------------------------------------------------------------------------------

  /** h("a", {class: "x", href: "#/", onclick: fn}, "text", child) -- text is always a text node. */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === undefined || v === null || v === false) return;
      if (k.slice(0, 2) === "on" && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }

  function add(el, child) {
    if (child === undefined || child === null || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { add(el, c); }); return; }
    el.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  function $(id) { return document.getElementById(id); }

  function storage(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* private mode: nothing persists */ }
    return null;
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var a = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, a); }, ms);
    };
  }

  function toast(text, action) {
    var box = $("toasts");
    var el = h("div", { class: "toast", role: "status" }, h("span", { text: text }));
    if (action) {
      el.appendChild(h("button", {
        class: "toast-action", type: "button", text: action.label,
        onclick: function () { el.remove(); action.run(); },
      }));
    }
    box.appendChild(el);
    while (box.childNodes.length > 2) box.removeChild(box.firstChild);
    setTimeout(function () { el.classList.add("out"); }, action ? 9000 : 3500);
    setTimeout(function () { el.remove(); }, action ? 9400 : 3900);
  }

  function sheet(title, body, onClose) {
    var root = $("sheet");
    function close() {
      root.hidden = true;
      clear(root);
      document.removeEventListener("keydown", onKey);
      if (onClose) onClose();
    }
    function onKey(e) { if (e.key === "Escape") close(); }
    clear(root);
    root.appendChild(h("div", { class: "sheet-backdrop", onclick: close }));
    root.appendChild(h("div", { class: "sheet-panel", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("div", { class: "sheet-head" },
        h("h2", { text: title }),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close", text: "✕", onclick: close })),
      h("div", { class: "sheet-body" }, body)));
    root.hidden = false;
    document.addEventListener("keydown", onKey);
    return close;
  }

  function img(url, cls, alt) {
    var src = imageSrc(url);
    if (!src) return h("div", { class: (cls || "") + " art-empty", "aria-hidden": "true" });
    return h("img", { class: cls, src: src, alt: alt || "", loading: "lazy", decoding: "async" });
  }

  function link(path, query, text, cls) {
    return h("a", { href: formatHash(path, query), class: cls, text: text });
  }

  function chip(label, active, onclick, count) {
    return h("button", { type: "button", class: "chip" + (active ? " on" : ""), "aria-pressed": active ? "true" : "false",
      onclick: onclick }, label, count !== undefined ? h("span", { class: "chip-n", text: String(count) }) : null);
  }

  function empty(text) { return h("p", { class: "empty", text: text }); }

  // ---- auth and API ---------------------------------------------------------------------------------------

  var state = { token: null, source: null, info: null, players: [], playerId: storage("cma_player"), queue: null,
    keep: false };

  async function probe(token) {
    try {
      var r = await fetch("../auth/me", { headers: token ? { Authorization: "Bearer " + token } : {},
        credentials: "same-origin", cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch (e) {
      return null;
    }
  }

  /** Ingress first (no token), then our own token, then MA's web UI token on this origin (never written). */
  async function authenticate() {
    var taken = takeCode(location.href, storage("cma_login_state"));
    if (taken.url !== location.href) history.replaceState(null, "", taken.url);
    storage("cma_login_state", null);
    if (taken.code) {
      storage("cma_token", taken.code);
      storage("cma_no_borrow", null);
    }
    var own = storage("cma_token");
    var borrowed = storage("cma_no_borrow") ? null : storage("ma_access_token");
    var order = probeOrder(location.pathname, own, borrowed);
    for (var i = 0; i < order.length; i++) {
      var src = order[i], token = src === "own" ? own : src === "borrowed" ? borrowed : null;
      if (await probe(token)) { state.token = token; state.source = src; return true; }
      if (src === "own") storage("cma_token", null);
    }
    return false;
  }

  function startLogin() {
    var nonce = "";
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    for (var i = 0; i < bytes.length; i++) nonce += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    storage("cma_login_state", nonce);
    location.href = "../login?return_url=" + encodeURIComponent(loginReturnUrl(location.origin, location.pathname,
      nonce));
  }

  var seq = 0;

  async function call(command, args) {
    var headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    var r;
    try {
      r = await fetch("../api", { method: "POST", headers: headers, credentials: "same-origin", cache: "no-store",
        body: JSON.stringify({ command: command, args: args || {}, message_id: "cma-" + (++seq) }) });
    } catch (e) {
      throw new Error("Can't reach Music Assistant.");
    }
    if (r.ok) {
      var text = await r.text();
      return text ? JSON.parse(text) : null;
    }
    var body = await r.text().catch(function () { return ""; });
    if (r.status === 401) {
      if (state.source === "own") storage("cma_token", null);
      state.token = null;
      showLogin();
    }
    var err = new Error(errorText(r.status, body));
    err.status = r.status === 400 && body.trim() === "Not found" ? 404 : r.status;
    throw err;
  }

  async function signOut() {
    if (state.source === "own" && state.token) {
      try {
        await fetch("../auth/logout", { method: "POST", headers: { Authorization: "Bearer " + state.token } });
      } catch (e) { /* the token expires on its own */ }
      storage("cma_token", null);
    }
    // a borrowed token belongs to MA's web UI: leave it alone, but stop using it until the next sign-in here
    if (state.source === "borrowed") storage("cma_no_borrow", "1");
    state.token = null;
    state.source = null;
    showLogin();
  }

  function showLogin() {
    stopPolling();
    $("app").hidden = true;
    var box = clear($("login"));
    box.hidden = false;
    box.appendChild(h("div", { class: "login-card" },
      h("img", { src: "icon.svg", alt: "", class: "login-icon" }),
      h("h1", { text: "Classical" }),
      h("p", { text: "Browse your classical library by composer, work and performer." }),
      h("button", { type: "button", class: "btn primary", text: "Sign in to Music Assistant", onclick: startLogin }),
      h("p", { class: "fine", text: "You'll come straight back here after signing in." })));
  }

  // ---- players and playback -------------------------------------------------------------------------------

  async function loadPlayers() {
    try {
      var all = await call("players/all");
      state.players = (all || []).filter(function (p) { return p.available !== false && p.enabled !== false; })
        .sort(function (a, b) { return cmp(fold(playerName(a)), fold(playerName(b))); });
    } catch (e) {
      state.players = [];
    }
    state.playerId = choosePlayer(state.players, storage("cma_player"));
    renderPlayerBar();
  }

  function pickPlayer(then) {
    var body = h("div", { class: "list" });
    if (!state.players.length) body.appendChild(empty("No players are available to your account."));
    state.players.forEach(function (p) {
      body.appendChild(h("button", { type: "button", class: "row row-btn" + (p.player_id === state.playerId ? " on" : ""),
        onclick: function () {
          state.playerId = p.player_id;
          storage("cma_player", p.player_id);
          close();
          refreshQueue();
          if (then) then();
        } },
      h("span", { class: "row-main" }, h("span", { class: "row-title", text: playerName(p) })),
      p.player_id === state.playerId ? h("span", { class: "tick", text: "✓" }) : null));
    });
    var close = sheet("Play on", body);
  }

  async function queueId() {
    if (!state.playerId) {
      return new Promise(function (resolve, reject) {
        pickPlayer(function () { queueId().then(resolve, reject); });
      });
    }
    var q = await call("player_queues/get_active_queue", { player_id: state.playerId });
    return (q && q.queue_id) || state.playerId;
  }

  async function withMovements(rec) {
    if (rec.movements) return rec;
    var full = await call("classical/recording", { id: rec.id });
    rec.movements = full.movements;
    return rec;
  }

  /** option: replace (from movement `from`), add or next. */
  async function play(rec, from, option) {
    try {
      option = option || "replace";
      if (from || option !== "replace") await withMovements(rec);
      var qid = await queueId();
      if (option !== "replace") {
        var q = await call("player_queues/get", { queue_id: qid });
        if (q && q.shuffle_enabled) {
          toast("Queue shuffle is on — movements may be mixed.", {
            label: "Turn off",
            run: function () {
              call("player_queues/shuffle", { queue_id: qid, shuffle_enabled: false })
                .then(function () { toast("Shuffle off"); }, function (e) { toast(e.message); });
            },
          });
        }
      }
      await call("player_queues/play_media", playArgs(qid, rec, from, option));
      toast(option === "add" ? "Added to queue" : option === "next" ? "Plays next" : "Playing");
      setTimeout(refreshQueue, 800);
    } catch (e) {
      toast(e.message);
    }
  }

  var pollTimer = null;

  async function refreshQueue() {
    if (!state.playerId || state.token === null && state.source !== "ingress") return;
    try {
      var qid = await queueId();
      state.queue = await call("player_queues/get", { queue_id: qid });
    } catch (e) {
      state.queue = null;
    }
    renderPlayerBar();
  }

  function startPolling() {
    stopPolling();
    if (document.visibilityState === "hidden") return;
    refreshQueue();
    pollTimer = setInterval(refreshQueue, 5000);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function renderPlayerBar() {
    var bar = clear($("player"));
    var p = state.players.filter(function (x) { return x.player_id === state.playerId; })[0];
    var q = state.queue;
    var item = q && q.current_item;
    var playing = q && q.state === "playing";
    var title = item ? (item.name || (item.media_item && item.media_item.name) || "") : "";
    bar.appendChild(h("button", { type: "button", class: "player-who", onclick: function () { pickPlayer(); } },
      h("span", { class: "player-icon", "aria-hidden": "true", text: "♫" }),
      h("span", { class: "player-text" },
        h("span", { class: "player-title", text: title || (p ? "Nothing playing" : "Choose a player") }),
        h("span", { class: "player-sub", text: p ? playerName(p) : "Tap to pick where music plays" }))));
    if (p) {
      bar.appendChild(h("button", { type: "button", class: "icon-btn big", "aria-label": playing ? "Pause" : "Play",
        text: playing ? "❚❚" : "▶",
        onclick: async function () {
          try { await call("player_queues/play_pause", { queue_id: await queueId() }); } catch (e) { toast(e.message); }
          setTimeout(refreshQueue, 400);
        } }));
      bar.appendChild(h("button", { type: "button", class: "icon-btn", "aria-label": "Next", text: "⏭",
        onclick: async function () {
          try { await call("player_queues/next", { queue_id: await queueId() }); } catch (e) { toast(e.message); }
          setTimeout(refreshQueue, 400);
        } }));
    }
  }

  // ---- router ---------------------------------------------------------------------------------------------

  var TABS = [
    { id: "today", label: "Today", icon: "☀" },
    { id: "composers", label: "Composers", icon: "♩" },
    { id: "works", label: "Works", icon: "♪" },
    { id: "performers", label: "Performers", icon: "♫" },
    { id: "review", label: "Review", icon: "✓" },
  ];
  var TAB_OF = { today: "today", composers: "composers", composer: "composers", works: "works", work: "works", recording: "works",
    boxes: "works", box: "works", albums: "works", performers: "performers", performer: "performers", review: "review" };
  var VIEWS = {};
  var renderId = 0;

  function renderTabs(active) {
    var nav = clear($("tabs"));
    TABS.forEach(function (t) {
      nav.appendChild(h("a", { href: "#/" + t.id, class: "tab" + (t.id === active ? " on" : ""),
        "aria-current": t.id === active ? "page" : null },
      h("span", { class: "tab-icon", "aria-hidden": "true", text: t.icon }),
      h("span", { class: "tab-label", text: t.label })));
    });
  }

  async function render() {
    var route = parseHash(location.hash);
    var name = route.path[0] || "today";
    var view = VIEWS[name] || VIEWS.today;
    renderTabs(TAB_OF[name] || "today");
    var id = ++renderId;
    var main = $("view");
    main.setAttribute("aria-busy", "true");
    var loading = setTimeout(function () {
      if (id === renderId) clear(main).appendChild(h("div", { class: "loading", text: "Loading…" }));
    }, 150);
    try {
      var el = await view(route.path.slice(1), route.query);
      if (id !== renderId) return;
      // a filter edit re-renders in place: keep the scroll position and the focused field
      var keep = state.keep, act = document.activeElement, label = null, caret = null;
      state.keep = false;
      if (keep && act && main.contains(act)) {
        label = act.getAttribute("aria-label");
        try { caret = act.selectionStart; } catch (e) { caret = null; }
      }
      clear(main).appendChild(el);
      if (label) {
        var again = Array.prototype.filter.call(main.querySelectorAll("[aria-label]"), function (x) {
          return x.getAttribute("aria-label") === label;
        })[0];
        if (again) {
          again.focus({ preventScroll: true });
          try { if (caret !== null) again.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
        }
      }
      document.title = (main.querySelector("h1") ? main.querySelector("h1").textContent + " – " : "") +
        "Classical";
      if (!keep) window.scrollTo(0, 0);
    } catch (e) {
      if (id !== renderId) return;
      clear(main).appendChild(h("div", { class: "error-box" },
        h("h1", { text: e.status === 404 ? "Not found" : "Couldn't load this" }),
        h("p", { text: e.message }),
        h("a", { class: "btn", href: "#/composers", text: "Back to composers" })));
    } finally {
      clearTimeout(loading);
      main.removeAttribute("aria-busy");
    }
  }

  function go(path, query, replace) {
    var hash = formatHash(path, query);
    if (replace) { state.keep = true; history.replaceState(null, "", hash); render(); }
    else location.hash = hash;
  }

  function header(title, sub, back) {
    return h("header", { class: "page-head" },
      back ? h("a", { class: "back", href: back.href, text: "‹ " + back.label }) : null,
      h("h1", { text: title }),
      sub ? h("p", { class: "sub", text: sub }) : null);
  }

  // ---- today ----------------------------------------------------------------------------------------------

  /** PLAN.md §5.9: the day's shelves (on this day, season, weekday, acclaimed); each pick plays a whole work. */
  VIEWS.today = async function () {
    var res = await call("classical/today", { date: localDate() });
    var wrap = h("div", { class: "page" }, header(res.label || "Today", daySubtitle(res)));
    var shelves = (res.shelves || []).filter(function (s) { return s.picks && s.picks.length; });
    if (!shelves.length) {
      wrap.appendChild(empty("Nothing to suggest yet. Shelves fill in once the index has works with recordings."));
      return wrap;
    }
    shelves.forEach(function (s) {
      var list = h("div", { class: "list" });
      s.picks.forEach(function (p) {
        var t = pickLines(p);
        list.appendChild(h("div", { class: "row pick" },
          img(p.image_url, "thumb"),
          h("a", { class: "row-main", href: formatHash(["work", p.work_id]) },
            h("span", { class: "row-title", text: t.title }),
            t.sub ? h("span", { class: "row-sub", text: t.sub }) : null,
            t.why ? h("span", { class: "pick-why", text: t.why }) : null),
          h("button", { type: "button", class: "icon-btn", "aria-label": "Play " + t.title, text: "▶",
            onclick: function () { play({ id: p.recording_id, play_uri: p.play_uri }); } })));
      });
      wrap.appendChild(h("section", { class: "shelf" },
        h("h2", { class: "shelf-head", text: s.title }),
        s.subtitle ? h("p", { class: "shelf-sub", text: s.subtitle }) : null,
        list));
    });
    return wrap;
  };

  // ---- composers ------------------------------------------------------------------------------------------

  VIEWS.composers = async function (args) {
    if (args[0]) return composerPage(args[0]);
    var res = await call("classical/composers");
    var groups = byLetter(res.items);
    var wrap = h("div", { class: "page" }, header("Composers", plural(res.items.length, "composer")));
    if (!groups.length) {
      wrap.appendChild(empty("No composers yet. The index fills in as Music Assistant scans your library."));
      return wrap;
    }
    var jump = h("nav", { class: "az", "aria-label": "Jump to letter" });
    groups.forEach(function (g) {
      jump.appendChild(h("button", { type: "button", text: g.letter, onclick: function () {
        var t = document.getElementById("letter-" + g.letter);
        if (t) t.scrollIntoView({ block: "start", behavior: "smooth" });
      } }));
    });
    wrap.appendChild(jump);
    groups.forEach(function (g) {
      var list = h("div", { class: "list" });
      g.items.forEach(function (c) {
        list.appendChild(h("a", { class: "row", href: formatHash(["composer", c.id]) },
          h("span", { class: "row-main" },
            h("span", { class: "row-title", text: c.name }),
            h("span", { class: "row-sub", text: [lifeDates(c.born, c.died), plural(c.works, "work")]
              .filter(Boolean).join(" · ") })),
          h("span", { class: "chev", "aria-hidden": "true", text: "›" })));
      });
      wrap.appendChild(h("section", { class: "letter", id: "letter-" + g.letter },
        h("h2", { class: "letter-head", text: g.letter }), list));
    });
    return wrap;
  };

  VIEWS.composer = function (args) { return composerPage(args[0]); };

  async function composerPage(cid) {
    var res = await call("classical/works", { composer_id: cid, limit: 1000 });
    var c = res.composer || {};
    var wrap = h("div", { class: "page" },
      header(c.name || "Composer", [lifeDates(c.born, c.died), plural(res.total, "work")].filter(Boolean)
        .join(" · "), { href: "#/composers", label: "Composers" }));
    var type = null;
    var chips = h("div", { class: "chips" });
    var list = h("div", { class: "list" });
    function draw() {
      clear(chips);
      if (res.types.length > 1) {
        chips.appendChild(chip("All", !type, function () { type = null; draw(); }, res.total));
        res.types.forEach(function (t) {
          chips.appendChild(chip(t.type || "Other", type === t.type, function () { type = t.type; draw(); }, t.n));
        });
      }
      clear(list);
      res.items.filter(function (w) { return !type || w.work_type === type; }).forEach(function (w) {
        list.appendChild(workRow(w, null, true));
      });
    }
    draw();
    wrap.appendChild(chips);
    wrap.appendChild(list);
    // the composer's albums, compilations included (filled in when they arrive; the works come first)
    var albums = h("section", { class: "section" });
    wrap.appendChild(albums);
    var asort = "title";
    function drawAlbums() {
      call("classical/albums", { composer_id: cid, sort: asort, limit: 1000 }).then(function (ar) {
        clear(albums);
        if (!ar.items.length) return;
        albums.appendChild(h("h2", null, "Albums (" + ar.total + ")"));
        albums.appendChild(h("div", { class: "chips" },
          chip("By title", asort === "title", function () { asort = "title"; drawAlbums(); }),
          chip("By year", asort === "year", function () { asort = "year"; drawAlbums(); })));
        var al = h("div", { class: "list box-discs" });
        ar.items.forEach(function (a) { al.appendChild(albumBlock(a, cid)); });
        albums.appendChild(al);
      }, function () { /* the section is optional */ });
    }
    drawAlbums();
    return wrap;
  }

  /** An album as a disclosure row: cover, name, year and other composers; inside, its recordings. */
  function albumBlock(a, cid) {
    var det = h("details", { class: "box-disc" },
      h("summary", { class: "row" }, img(a.image_url, "thumb"), h("span", { class: "row-main" },
        h("span", { class: "row-title", text: a.name || "Unknown album" }),
        h("span", { class: "row-sub", text: albumSubtitle(a, cid) }))));
    var inner = h("div", { class: "list box-disc-recs" });
    a.recordings.forEach(function (r) { inner.appendChild(boxRecRow(r, !cid, false)); });
    det.appendChild(inner);
    return det;
  }

  VIEWS.albums = async function (args, query) {
    var sort = ["title", "year"].indexOf(query.sort) >= 0 ? query.sort : "composer";
    var res = await call("classical/albums", { sort: sort, limit: 1000 });
    var wrap = h("div", { class: "page" }, header("Albums", plural(res.total, sort === "composer" ? "entry" : "album", sort === "composer" ? "entries" : null),
      { href: formatHash(["works"]), label: "Works" }));
    wrap.appendChild(h("div", { class: "chips" },
      chip("By composer", sort === "composer", function () { go(["albums"], {}, true); }),
      chip("By title", sort === "title", function () { go(["albums"], { sort: "title" }, true); }),
      chip("By year", sort === "year", function () { go(["albums"], { sort: "year" }, true); })));
    if (!res.items.length) wrap.appendChild(empty("No albums yet."));
    var sec = null, last = null;
    res.items.forEach(function (a) {
      var c = a.composer;
      if (!sec || (c && c.id !== last)) {
        sec = h("div", { class: "list box-discs" });
        if (c) wrap.appendChild(h("h2", { class: "letter-head" }, link(["composer", c.id], null, c.name)));
        wrap.appendChild(sec);
        last = c && c.id;
      }
      sec.appendChild(albumBlock(a, c ? c.id : null));
    });
    if (res.total > res.items.length) wrap.appendChild(h("p", { class: "fine", text: "Showing the first " + res.items.length + "." }));
    return wrap;
  };

  function workRow(w, scope, noComposer) {
    var sub = noComposer ? Object.assign({}, w, { composer: null }) : w;
    var count = w.mine !== undefined && w.mine !== w.recordings
      ? w.mine + " of " + plural(w.recordings, "recording") : plural(w.recordings, "recording");
    return h("a", { class: "row", href: formatHash(["work", w.id], { scope: scope }) },
      h("span", { class: "row-main" },
        h("span", { class: "row-title", text: w.title }),
        h("span", { class: "row-sub", text: [workSubtitle(sub), rowDate(w), count].filter(Boolean).join(" · ") })),
      h("span", { class: "chev", "aria-hidden": "true", text: "›" }));
  }

  // ---- works ----------------------------------------------------------------------------------------------

  VIEWS.works = async function (args, query) {
    var f = { composer_id: query.composer || null, work_type: query.type || null, q: query.q || null,
      date_kind: query.dk || "composed", date_from: num(query.from), date_to: num(query.to),
      performer_id: query.performer || null, period: query.period || null, decade: num(query.decade),
      sort: query.sort || null };
    var res = await call("classical/works", Object.assign({}, f, { limit: 200 }));
    var wrap = h("div", { class: "page" }, header("Works", plural(res.total, "work")));

    function update(patch) { go(["works"], worksQuery(query, patch), true); }

    var search = h("input", { type: "search", class: "input", placeholder: "Title, catalogue or key",
      value: query.q || "", "aria-label": "Search works", autocomplete: "off",
      oninput: debounce(function () { update({ q: search.value.trim() }); }, 350) });
    var typeSel = h("select", { class: "input", "aria-label": "Work type", onchange: function () {
      update({ type: typeSel.value });
    } }, h("option", { value: "", text: "All types" }));
    res.types.forEach(function (t) {
      typeSel.appendChild(h("option", { value: t.type || "", text: (t.type || "Other") + " (" + t.n + ")",
        selected: query.type && query.type === t.type ? true : null }));
    });
    var dk = h("select", { class: "input", "aria-label": "Date kind", onchange: function () {
      update({ dk: dk.value });
    } });
    [["composed", "Composed"], ["premiered", "Premiered"], ["recorded", "Recorded"], ["released", "Released"]].forEach(function (o) {
      dk.appendChild(h("option", { value: o[0], text: o[1], selected: f.date_kind === o[0] ? true : null }));
    });
    var from = h("input", { class: "input year", type: "text", inputmode: "numeric", pattern: "[0-9]*", maxlength: "4", placeholder: "From",
      value: query.from || "", "aria-label": "From year", min: "800", max: "2100" });
    var to = h("input", { class: "input year", type: "text", inputmode: "numeric", pattern: "[0-9]*", maxlength: "4", placeholder: "To",
      value: query.to || "", "aria-label": "To year", min: "800", max: "2100" });
    var years = debounce(function () { update({ from: from.value, to: to.value }); }, 600);
    from.addEventListener("input", years);
    to.addEventListener("input", years);

    var periodSel = h("select", { class: "input", "aria-label": "Period", onchange: function () {
      update({ period: periodSel.value, decade: "" });
    } }, h("option", { value: "", text: "Any period" }));
    (res.periods || []).forEach(function (p) {
      periodSel.appendChild(h("option", { value: p.period, text: p.label + " (" + p.n + ")",
        selected: f.period === p.period ? true : null }));
    });
    var decadeSel = h("select", { class: "input", "aria-label": "Decade", onchange: function () {
      update({ decade: decadeSel.value });
    } }, h("option", { value: "", text: "Any decade" }));
    (res.decades || []).forEach(function (d) {
      decadeSel.appendChild(h("option", { value: String(d.decade), text: d.decade + "s (" + d.n + ")",
        selected: f.decade === d.decade ? true : null }));
    });
    var sortSel = h("select", { class: "input", "aria-label": "Sort works", onchange: function () {
      update({ sort: sortSel.value });
    } });
    [["catalogue", "Sort: catalogue"], ["date", "Earliest first"], ["date_desc", "Latest first"]].forEach(function (o) {
      sortSel.appendChild(h("option", { value: o[0], text: o[1],
        selected: (f.sort || "catalogue") === o[0] ? true : null }));
    });

    var cov = res.date_coverage || {};
    var covText = f.date_kind === "composed"
      ? (cov.composed || 0) + " of " + plural(cov.total || 0, "work") + " have a composition date"
      : null;

    wrap.appendChild(h("div", { class: "filters" },
      search,
      h("div", { class: "filter-row" }, typeSel, sortSel),
      h("div", { class: "filter-row" }, periodSel, decadeSel),
      h("div", { class: "filter-row" }, dk, from, h("span", { class: "dash", text: "–" }), to),
      covText && (f.date_from || f.date_to || f.date_kind === "composed") ? h("p", { class: "fine", text: covText }) : null));

    if (res.composer || f.performer_id) {
      var tags = h("div", { class: "chips" });
      if (res.composer) {
        tags.appendChild(chip(res.composer.name + " ✕", true, function () { update({ composer: "" }); }));
      }
      if (f.performer_id) tags.appendChild(chip("Performer ✕", true, function () { update({ performer: "" }); }));
      wrap.appendChild(tags);
    }

    var boxSlot = h("div");
    wrap.appendChild(boxSlot);
    call("classical/boxes", {}).then(function (bx) {
      if (bx && bx.boxes && bx.boxes.length) {
        boxSlot.appendChild(link(["boxes"], null, "Box sets (" + bx.boxes.length + ") ›", "more-link"));
      }
    }, function () { /* the list is optional */ });
    wrap.appendChild(link(["albums"], null, "Albums by composer ›", "more-link"));

    var list = h("div", { class: "list" });
    res.items.forEach(function (w) { list.appendChild(workRow(w, f.performer_id)); });
    wrap.appendChild(list);
    if (!res.items.length) wrap.appendChild(empty("No works match these filters."));
    var offset = res.items.length;
    if (offset < res.total) {
      var more = h("button", { type: "button", class: "btn wide", text: "Load more", onclick: async function () {
        more.disabled = true;
        try {
          var next = await call("classical/works", Object.assign({}, f, { offset: offset, limit: 200 }));
          next.items.forEach(function (w) { list.appendChild(workRow(w, f.performer_id)); });
          offset += next.items.length;
          if (offset >= next.total || !next.items.length) more.remove();
        } catch (e) { toast(e.message); }
        more.disabled = false;
      } });
      wrap.appendChild(more);
    }
    return wrap;
  };

  function num(v) {
    var n = parseInt(v, 10);
    return isNaN(n) ? null : n;
  }

  // ---- work page ------------------------------------------------------------------------------------------

  VIEWS.work = async function (args, query) {
    var res = await call("classical/work", { id: args[0], scope: query.scope || null });
    var w = res.work;
    var recs = res.recordings;
    var withLocal = anyStreaming(recs);
    recs.forEach(function (r) { r._text = recordingText(r); });
    var byId = {};
    recs.forEach(function (r) { byId[r.id] = r; });
    var scope = res.scope ? res.scope.ids : [];
    var f = { q: "", people: {}, decade: null, rel_decade: null, scope: scope, all: false };
    var sortKey = "";
    var shown = PAGE;
    var expanded = {};

    var composers = (w.composers || []).map(function (c) { return c.name; }).join(", ");
    var wrap = h("div", { class: "page" });
    var back = (w.composers || [])[0];
    wrap.appendChild(header(w.title, [composers, w.catalogue, w.key, w.work_type].filter(function (x, i, a) {
      return x && a.indexOf(x) === i && (i === 0 || w.title.indexOf(x) < 0 || x === w.work_type);
    }).join(" · "), back ? { href: formatHash(["composer", back.id]), label: back.name } : null));

    var best = res.best && byId[res.best.recording_id];
    var actions = h("div", { class: "actions" });
    if (best) {
      actions.appendChild(h("button", { type: "button", class: "btn primary", onclick: function () { play(best); } },
        "▶ Play whole work"));
      actions.appendChild(h("p", { class: "fine best", text: best.label + " — " + res.best.reason }));
    }
    if (res.acclaim) {
      actions.appendChild(h("p", { class: "fine acclaim", text: plural(res.acclaim.recordings, "recording") +
        " with awards: " + res.acclaim.awards.map(function (a) { return a.award + (a.n > 1 ? " (" + a.n + ")" : ""); })
        .join(", ") }));
    }
    var dl = datesLine(res.work.dates);
    var canCorrect = (state.info || {}).can_correct;
    if (dl || canCorrect) {
      actions.appendChild(h("p", { class: "dates" },
        dl ? h("span", { text: dl.text }) : null,
        dl && dl.source ? h("span", { class: "dates-src", text: " · " + dl.source }) : null,
        canCorrect ? h("button", { type: "button", class: "link-btn", text: dl ? "Edit dates" : "Add dates",
          onclick: function () { datesSheet(w); } }) : null));
    }
    var ml = mbLinkLine(res.mb_link);
    if (ml && canCorrect) {
      var unlink = guarded("Unlink", "link-btn", async function () {
        try {
          var res = await call("classical/work_mb_link", { work_id: w.id, decision: "reject", mbid: ml.mbid });
          undoToast("Unlinked; not proposed again", res.batch);
          render();
        } catch (e) { toast(e.message); }
      }, true);
      actions.appendChild(h("p", { class: "fine mb-link" }, h("span", { text: ml.text + " " }),
        ml.href ? h("a", { href: ml.href, target: "_blank", rel: "noopener noreferrer", text: "MusicBrainz" }) : null,
        " ", unlink));
    }
    wrap.appendChild(actions);

    var count = h("p", { class: "count" });
    var scopeBar = h("div", { class: "scope" });
    var list = h("div", { class: "list recs" });
    var moreBtn = h("button", { type: "button", class: "btn wide", text: "Show more", onclick: function () {
      shown += PAGE; draw();
    } });

    // focus bar: text, facet selects, decade chips, sort
    var focus = h("div", { class: "filters" });
    if (recs.length > 1) {
      focus.appendChild(h("input", { type: "search", class: "input", placeholder: "Filter recordings",
        "aria-label": "Filter recordings", autocomplete: "off",
        oninput: debounce(function (e) { f.q = e.target.value; shown = PAGE; draw(); }, 200) }));
      var row = h("div", { class: "facet-grid" });
      BUCKETS.forEach(function (b) {
        var opts = res.facets[b] || [];
        if (!opts.length) return;
        var sel = h("select", { class: "input", "aria-label": BUCKET_TITLES[b], onchange: function () {
          f.people[b] = sel.value || null; shown = PAGE; draw();
        } }, h("option", { value: "", text: "Any " + BUCKET_TITLES[b].toLowerCase().replace(/s$/, "").replace("chamber", "ensemble") }));
        opts.forEach(function (o) {
          sel.appendChild(h("option", { value: o.id,
            text: o.name + (b === "soloists" && o.instrument ? ", " + o.instrument : "") + " (" + o.n + ")" }));
        });
        row.appendChild(sel);
      });
      var sortSel = h("select", { class: "input", "aria-label": "Sort", onchange: function () {
        sortKey = sortSel.value; draw();
      } });
      var acclaimed = anyAcclaim(recs);
      [["", "Sort: best first"], ["year", "Oldest first"], ["year_desc", "Newest first"],
        ["performer", "Performer A–Z"], ["duration", "Longest first"], ["acclaim", "Most acclaimed"]]
        .forEach(function (o) {
          var off = o[0] === "acclaim" && !acclaimed;
          sortSel.appendChild(h("option", { value: o[0], text: o[1] + (off ? " (no awards found)" : ""),
            disabled: off ? true : null }));
        });
      row.appendChild(sortSel);
      focus.appendChild(row);
      var decs = res.facets.decades.length ? res.facets.decades : [];
      if (decs.length > 1) {
        var dchips = h("div", { class: "chips scroll" });
        var drawDecades = function () {
          clear(dchips);
          dchips.appendChild(chip("Any year", !f.decade, function () { f.decade = null; shown = PAGE; drawDecades(); draw(); }));
          decs.forEach(function (d) {
            dchips.appendChild(chip(d.decade + "s", f.decade === d.decade, function () {
              f.decade = f.decade === d.decade ? null : d.decade; shown = PAGE; drawDecades(); draw();
            }, d.n));
          });
        };
        drawDecades();
        focus.appendChild(dchips);
      }
      wrap.appendChild(focus);
    }
    wrap.appendChild(scopeBar);
    wrap.appendChild(count);
    wrap.appendChild(list);
    wrap.appendChild(moreBtn);

    if (res.contained_tracks && res.contained_tracks.length) {
      var ct = h("div", { class: "list" });
      res.contained_tracks.forEach(function (t) {
        ct.appendChild(h("div", { class: "row" }, h("span", { class: "row-main" },
          h("span", { class: "row-title", text: t.title }),
          h("span", { class: "row-sub", text: "Excerpt on another release" }))));
      });
      wrap.appendChild(h("section", { class: "section" }, h("h2", { text: "Also contains this work" }), ct));
    }
    ((state.info || {}).streaming || []).forEach(function (p) { wrap.appendChild(streamingSection(w, p)); });

    function drawScope() {
      clear(scopeBar);
      if (!res.scope) return;
      var names = res.scope.people.map(function (p) { return p.name; }).join(" + ");
      scopeBar.appendChild(h("span", { text: f.all ? "Showing all recordings" : "With " + names }));
      scopeBar.appendChild(h("button", { type: "button", class: "chip on", text: f.all ? "Only " + names : "SHOW ALL",
        onclick: function () { f.all = !f.all; shown = PAGE; draw(); } }));
    }

    function draw() {
      drawScope();
      var hits = sortRecordings(filterRecordings(recs, f), sortKey);
      count.textContent = hits.length === recs.length ? plural(recs.length, "recording")
        : hits.length + " of " + plural(recs.length, "recording");
      clear(list);
      hits.slice(0, shown).forEach(function (r) { list.appendChild(recRow(r, expanded, withLocal)); });
      if (!hits.length) list.appendChild(empty("No recordings match."));
      moreBtn.hidden = hits.length <= shown;
    }
    draw();
    return wrap;
  };

  /** "Other recordings on Qobuz" (§5.10): searched only when asked, never added without the button. */
  function streamingSection(w, prov) {
    var sec = h("section", { class: "section streaming" }, h("h2", { text: "Other recordings on " + prov.name }));
    var body = h("div", { class: "list" });
    var btn = h("button", { type: "button", class: "btn", text: "Search " + prov.name, onclick: search });
    sec.appendChild(btn);
    sec.appendChild(body);
    async function search() {
      btn.disabled = true;
      clear(body).appendChild(h("div", { class: "loading small", text: "Searching " + prov.name + "…" }));
      var res;
      try {
        res = await call("classical/streaming", { work_id: w.id, provider: prov.instance });
      } catch (e) {
        clear(body).appendChild(empty(e.message || "Search failed."));
        btn.disabled = false;
        return;
      }
      btn.hidden = true;
      clear(body);
      var entry = (res.providers || [])[0] || { albums: [] };
      if (entry.error) { body.appendChild(empty(entry.error + ".")); btn.hidden = false; btn.disabled = false; return; }
      if (!entry.albums.length) { body.appendChild(empty("Nothing else found on " + prov.name + ".")); return; }
      entry.albums.forEach(function (a) { body.appendChild(streamingRow(a)); });
    }
    return sec;
  }

  function streamingRow(a) {
    var canAdd = !!(state.info || {}).can_add && !!a.item_id;
    var add = canAdd ? h("button", { type: "button", class: "btn small", text: "Add to library",
      onclick: async function () {
        add.disabled = true;
        add.textContent = "Adding…";
        try {
          await call("classical/add_album", { provider: a.provider_instance, item_id: a.item_id });
          add.textContent = "Added — indexing";
        } catch (e) {
          add.disabled = false;
          add.textContent = "Add to library";
          toast(e.message || "Could not add the album.");
        }
      } }) : null;
    return h("div", { class: "row" }, img(a.image_url, "thumb"),
      h("span", { class: "row-main" },
        h("span", { class: "row-title", text: a.name }),
        h("span", { class: "row-sub", text: [a.performers.slice(0, 3).join(", ") || a.artist, a.year,
          plural(a.tracks, "track")].filter(Boolean).join(" · ") })),
      add);
  }

  function sourceBadges(r, withLocal) {
    var list = sourceLabels(r.sources, withLocal);
    if (!list.length) return null;
    var box = h("span", { class: "badges sources" });
    list.forEach(function (s) {
      box.appendChild(h("span", { class: "badge source" + (s.streaming ? " streaming" : ""), text: s.text,
        title: s.streaming ? "Plays from " + s.text : "Plays from your files" }));
    });
    return box;
  }

  function recRow(r, expanded, withLocal) {
    var box = h("div", { class: "rec" + (expanded[r.id] ? " open" : "") });
    var detail = h("div", { class: "rec-detail" });
    var head = h("button", { type: "button", class: "rec-head", "aria-expanded": expanded[r.id] ? "true" : "false",
      onclick: function () {
        expanded[r.id] = !expanded[r.id];
        box.classList.toggle("open", !!expanded[r.id]);
        head.setAttribute("aria-expanded", expanded[r.id] ? "true" : "false");
        if (expanded[r.id]) fillDetail();
      } },
    img(r.image_url, "thumb"),
    h("span", { class: "row-main" },
      h("span", { class: "row-title", text: r.who || r.album || "Unknown performers" }),
      h("span", { class: "row-sub", text: [yearText(r), boxLine(r.box) || r.album, r.tracks ? plural(r.tracks, "track") : null]
        .filter(Boolean).join(" · ") }),
      badges(r.accolades, 2), sourceBadges(r, withLocal)),
    h("span", { class: "dur", text: r.duration ? r.duration_text : "" }));
    var filled = false;
    async function fillDetail() {
      if (filled) return;
      filled = true;
      clear(detail).appendChild(h("div", { class: "loading small", text: "Loading…" }));
      try {
        await withMovements(r);
        clear(detail).appendChild(recordingBody(r, true));
      } catch (e) {
        filled = false;
        clear(detail).appendChild(empty(e.message));
      }
    }
    box.appendChild(head);
    box.appendChild(detail);
    if (expanded[r.id]) fillDetail();
    return box;
  }

  /** Award badges; album-level and other-release ones say so. `max`: show that many, then "+n". */
  function badges(accs, max) {
    var list = (accs || []).filter(function (a) { return a.kind === "award"; });
    if (!list.length) return null;
    var box = h("span", { class: "badges" });
    list.slice(0, max || list.length).forEach(function (a) {
      box.appendChild(h("span", { class: "badge" + (a.result === "nominee" ? " nominee" : "") +
        (a.counts ? "" : " album") + (a.grade === "other_release" ? " other" : ""),
      title: badgeText(a, true), text: a.text + (a.counts ? "" : " (album)") + (a.grade === "other_release" ? " *" : "") }));
    });
    if (max && list.length > max) box.appendChild(h("span", { class: "badge more", text: "+" + (list.length - max) }));
    return box;
  }

  function extLink(url, text) {
    var href = safeHref(url);
    if (!href) return h("span", { text: text });
    return h("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: text });
  }

  /** The recording page's awards, reviews, ratings and "Read reviews" links (links only: nothing fetched). */
  function accoladesSection(r) {
    var sec = h("section", { class: "section accolades" }, h("h2", { text: "Awards & reviews" }));
    var awards = r.awards || [];
    if (awards.length) {
      var ul = h("ul", { class: "awards" });
      awards.forEach(function (a) {
        ul.appendChild(h("li", null, h("span", { class: "badge" + (a.result === "nominee" ? " nominee" : ""), text: a.text }),
          a.category ? h("span", { text: " " + a.category }) : null,
          a.note ? h("span", { class: "fine", text: " — " + a.note }) : null,
          a.url ? h("span", { class: "fine" }, " · ", extLink(a.url, "source")) : null));
      });
      sec.appendChild(ul);
    } else {
      sec.appendChild(h("p", { class: "fine", text: "No awards found for this recording." }));
    }
    (r.reviews || []).forEach(function (v) {
      sec.appendChild(h("blockquote", { class: "review" },
        v.excerpt ? h("p", { text: v.excerpt }) : null,
        h("p", { class: "fine" }, [v.publication, v.reviewer, v.rating ? v.rating + "/" + (v.rating_scale || 5) : null]
          .filter(Boolean).join(" · ") + (v.excerpt_license ? " · " + v.excerpt_license : "") + " · ",
        extLink(v.url, "full review"))));
    });
    (r.ratings || []).forEach(function (v) {
      sec.appendChild(h("p", { class: "fine" }, v.text + (v.rating_count ? " (" + plural(v.rating_count, "rating") + ")" : "") +
        (v.counts ? "" : " · for the album") + " · ", extLink(v.url, v.publication || "source")));
    });
    var links = r.review_links || [];
    if (links.length) {
      var rl = h("p", { class: "review-links" }, h("span", { class: "fine", text: "Read reviews: " }));
      links.forEach(function (l, i) {
        if (i) rl.appendChild(document.createTextNode(" · "));
        rl.appendChild(extLink(l.url, l.site + (l.kind === "search" ? " (search)" : "")));
      });
      sec.appendChild(rl);
    }
    return sec;
  }

  function playButtons(r) {
    return h("div", { class: "actions row-actions" },
      h("button", { type: "button", class: "btn primary", onclick: function () { play(r); } }, "▶ Play"),
      h("button", { type: "button", class: "btn", onclick: function () { play(r, 0, "next"); } }, "Play next"),
      h("button", { type: "button", class: "btn", onclick: function () { play(r, 0, "add"); } }, "Add to queue"));
  }

  function recordingBody(r, compact) {
    var body = h("div", { class: "rec-body" });
    body.appendChild(playButtons(r));
    var groups = creditGroups(r.credits);
    if (groups.length) {
      var dl = h("dl", { class: "credits" });
      groups.forEach(function (g) {
        dl.appendChild(h("dt", { text: g.title }));
        var dd = h("dd");
        g.people.forEach(function (p, i) {
          if (i) dd.appendChild(document.createTextNode(", "));
          dd.appendChild(link(["performer", p.id], null, p.name));
          if (p.role) dd.appendChild(h("span", { class: "role", text: " (" + p.role + ")" }));
        });
        dl.appendChild(dd);
      });
      body.appendChild(dl);
    } else {
      body.appendChild(h("p", { class: "fine", text: "No performer credits in the tags." }));
    }
    var movs = r.movements || [];
    if (movs.length) {
      var ol = h("ol", { class: "movements" });
      movs.forEach(function (m, k) {
        ol.appendChild(h("li", null, h("button", { type: "button", class: "mov",
          "aria-label": "Play from " + (m.name || m.title), onclick: function () { play(r, k); } },
        h("span", { class: "mov-n", text: m.movement_no ? String(m.movement_no) : String(k + 1) }),
        h("span", { class: "mov-name", text: m.name || m.title }),
        h("span", { class: "dur", text: m.duration ? m.duration_text : "" }))));
      });
      body.appendChild(h("p", { class: "fine", text: "Tap a movement to play from there." }));
      body.appendChild(ol);
    }
    var facts = [];
    if (r.recorded) facts.push(["Recorded", String(r.recorded)]);
    if (r.release_year) facts.push(["Released", String(r.release_year)]);
    if (r.venue) facts.push(["Venue", r.venue]);
    if (r.album) facts.push(["Album", r.album + (r.album_artist ? " — " + r.album_artist : "")]);
    if (r.box) facts.push(["Box set", r.box.title + (r.box.disc != null ? ", CD " + r.box.disc : "")]);
    facts.push(["Length", r.duration_text + (r.complete ? " · complete" : "")]);
    var fl = h("dl", { class: "facts" });
    facts.forEach(function (x) { fl.appendChild(h("dt", { text: x[0] })); fl.appendChild(h("dd", { text: x[1] })); });
    body.appendChild(fl);
    if (compact) body.appendChild(link(["recording", r.id], null, "Recording details ›", "more-link"));
    return body;
  }

  // ---- recording page -------------------------------------------------------------------------------------

  VIEWS.recording = async function (args) {
    var r = await call("classical/recording", { id: args[0] });
    var w = r.work || {};
    var wrap = h("div", { class: "page" });
    wrap.appendChild(header(r.who || "Recording", w.title,
      w.id ? { href: formatHash(["work", w.id]), label: w.title } : null));
    var album = r.album_info || {};
    wrap.appendChild(h("div", { class: "album-card" },
      img(album.image_url || r.image_url, "cover", album.name),
      h("div", null,
        h("p", { class: "album-name", text: album.name || r.album || "" }),
        h("p", { class: "fine", text: [album.artist, album.year].filter(Boolean).join(" · ") }),
        r.box ? link(["box", r.box.id], null, boxLine(r.box) + " ›", "more-link") : null,
        badges(r.accolades), sourceBadges(r, true))));
    wrap.appendChild(recordingBody(r, false));
    wrap.appendChild(accoladesSection(r));
    if (r.merged && (state.info || {}).can_correct) {
      wrap.appendChild(h("section", { class: "section" },
        h("p", { class: "fine", text: "This album's " + (w.title || "work") +
          " was matched to the same work on other albums." }),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", text: "Keep separate",
          onclick: function () { separateSheet(r, w); } }))));
    }
    if (r.discs && r.discs.length > 1 && (state.info || {}).can_correct) {
      wrap.appendChild(h("section", { class: "section" },
        h("p", { class: "fine", text: "This recording continues across " + plural(r.discs.length, "disc") + ": " +
          r.discs.map(function (d) { return d.name; }).join(", ") + "." }),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", text: "Keep discs separate",
          onclick: function () { separateDiscsSheet(r); } }))));
    }
    return wrap;
  };

  // ---- box sets -------------------------------------------------------------------------------------------

  VIEWS.boxes = async function () {
    var res = await call("classical/boxes", {});
    var wrap = h("div", { class: "page" }, header("Box sets", plural(res.boxes.length, "box set"),
      { href: formatHash(["works"]), label: "Works" }));
    var list = h("div", { class: "list" });
    res.boxes.forEach(function (b) {
      list.appendChild(h("a", { class: "row", href: formatHash(["box", b.id]) }, img(b.image_url, "thumb"),
        h("span", { class: "row-main" }, h("span", { class: "row-title", text: b.title }),
          h("span", { class: "row-sub", text: boxSummary(b) })),
        h("span", { class: "chev", "aria-hidden": "true", text: "›" })));
    });
    wrap.appendChild(list);
    if (!res.boxes.length) wrap.appendChild(empty("No box sets found. A box set is one album with many discs and works, or albums titled like “Complete Works CD 12”."));
    return wrap;
  };

  function boxRecRow(r, showComposer, showDisc) {
    var title = (showComposer && r.composer ? r.composer + ": " : "") + r.title;
    return h("div", { class: "row box-rec" },
      h("a", { class: "row-main", href: formatHash(["recording", r.id]) },
        h("span", { class: "row-title", text: title }),
        h("span", { class: "row-sub", text: [r.who, yearText(r), showDisc && r.disc != null ? "CD " + r.disc : null]
          .filter(Boolean).join(" · ") })),
      h("span", { class: "dur", text: r.duration ? r.duration_text : "" }),
      h("button", { type: "button", class: "icon-btn", "aria-label": "Play " + r.title,
        onclick: function () { play(r); } }, "▶"));
  }

  VIEWS.box = async function (args, query) {
    var res = await call("classical/box", { id: args[0] });
    var b = res.box;
    var byComposer = query.by === "composer";
    var wrap = h("div", { class: "page" }, header(b.title, boxSummary(b),
      { href: formatHash(["boxes"]), label: "Box sets" }));
    if (b.image_url || b.artist) {
      wrap.appendChild(h("div", { class: "album-card" }, img(b.image_url, "cover", b.title),
        h("div", null, h("p", { class: "album-name", text: b.title }),
          h("p", { class: "fine", text: b.artist || "" }))));
    }
    wrap.appendChild(h("div", { class: "chips" },
      chip("By disc", !byComposer, function () { go(["box", b.id], {}, true); }),
      chip("By composer", byComposer, function () { go(["box", b.id], { by: "composer" }, true); })));
    var multi = (b.composers || []).length > 1;
    if (byComposer) {
      res.composers.forEach(function (c) {
        var sec = h("section", { class: "section" },
          h("h2", null, c.id ? link(["composer", c.id], null, c.name) : c.name));
        c.works.forEach(function (w) {
          sec.appendChild(h("p", { class: "mini" }, link(["work", w.id], null,
            w.title + (w.catalogue && w.title.indexOf(w.catalogue) < 0 ? ", " + w.catalogue : ""))));
          var list = h("div", { class: "list" });
          w.recordings.forEach(function (r) { list.appendChild(boxRecRow(r, false, true)); });
          sec.appendChild(list);
        });
        wrap.appendChild(sec);
      });
    } else {
      var list = h("div", { class: "list box-discs" });
      res.discs.forEach(function (d, i) {
        var det = h("details", { class: "box-disc", open: res.discs.length <= 3 || i === 0 ? true : null },
          h("summary", { class: "row" }, h("span", { class: "row-main" },
            h("span", { class: "row-title", text: discTitle(d) }),
            h("span", { class: "row-sub", text: d.recordings.map(function (r) { return r.title; })
              .filter(function (t, k, a) { return a.indexOf(t) === k; }).join(" · ") }))));
        var inner = h("div", { class: "list box-disc-recs" });
        d.recordings.forEach(function (r) { inner.appendChild(boxRecRow(r, multi, false)); });
        det.appendChild(inner);
        list.appendChild(det);
      });
      wrap.appendChild(list);
    }
    if ((state.info || {}).can_correct) {
      wrap.appendChild(h("section", { class: "section" },
        h("p", { class: "fine", text: "Grouped as a box set from the album titles and disc numbers." }),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", text: "Not a box set",
          onclick: function () { notBoxSheet(b); } }))));
    }
    return wrap;
  };

  function notBoxSheet(b) {
    var body = h("div", null,
      h("p", { text: "These discs will be listed as ordinary albums again. Future scans keep this choice." }),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", text: "Not a box set", onclick: async function () {
          try {
            var res = await call("classical/box_decide", { box_id: b.id, decision: "not_box" });
            close();
            undoToast("Not a box set", res.batch);
            go(["boxes"], {}, true);
          } catch (e) { toast(e.message); }
        } }),
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Not a box set?", body);
  }

  function separateDiscsSheet(r) {
    var body = h("div", null,
      h("p", { text: "Each disc will be listed as its own recording. Future scans keep this choice." }),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", text: "Keep discs separate", onclick: async function () {
          try {
            var res = await call("classical/separate_discs", { recording_id: r.id });
            close();
            undoToast("Discs kept separate", res.batch);
            render();
          } catch (e) { toast(e.message); }
        } }),
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Not one recording?", body);
  }

  /** A correction of a work's dates; it outlives re-indexing. Blank means "use the automatic value". */
  function datesSheet(w) {
    var d = w.dates || {};
    var mine = function (part) { return part && part.source === "user"; };
    var hidden = function (kind) { return (d.hidden || []).indexOf(kind) >= 0 ? "unknown" : ""; };
    var composed = h("input", { class: "input", type: "text", "aria-label": "Composed",
      placeholder: "e.g. 1804–05, c. 1805, 1860s", value: mine(d.composed) ? d.composed.text : hidden("composed") });
    var premiered = h("input", { class: "input", type: "text", "aria-label": "Premiered",
      placeholder: "e.g. 1805-11-20 or 1805", value: mine(d.premiered) ? d.premiered.date : hidden("premiered") });
    var place = h("input", { class: "input", type: "text", "aria-label": "Premiere place",
      placeholder: "Place (optional)", value: mine(d.premiered) ? d.premiered.place || "" : "" });
    var now = h("p", { class: "fine", text: "Now: " + ((datesLine(d) || {}).text || "no dates") +
      ". Leave a field blank to use the automatic value; type “unknown” to hide it." });
    var body = h("div", null, now,
      h("label", { class: "mini", text: "Composed" }), composed,
      h("label", { class: "mini", text: "Premiered" }), premiered, place,
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", text: "Save", onclick: async function () {
          try {
            await call("classical/work_dates", { work_id: w.id, composed: composed.value.trim(),
              premiered: premiered.value.trim(), premiered_place: place.value.trim() });
            close();
            toast("Dates saved");
            render();
          } catch (e) { toast(e.message); }
        } }),
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Dates of " + (w.title || "this work"), body);
  }

  function separateSheet(r, w) {
    var body = h("div", null,
      h("p", { text: "This recording will be listed under its own work instead of " + (w.title || "this work") +
        ". Future scans keep this choice." }),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", text: "Keep separate", onclick: async function () {
          try {
            await call("classical/separate_work", { recording_id: r.id });
            close();
            toast("Kept separate; the album will be re-indexed");
            render();
          } catch (e) { toast(e.message); }
        } }),
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Not the same work?", body);
  }

  // ---- performers -----------------------------------------------------------------------------------------

  VIEWS.performers = async function (args, query) {
    var role = args[0] || "all";
    var res = await call("classical/performers", { role: role, with_id: query.with || null, q: query.q || null });
    var wrap = h("div", { class: "page" }, header("Performers",
      res.with ? "Recorded with " + res.with.name : plural(res.items.length, "performer")));
    var chips = h("div", { class: "chips scroll" });
    ["all"].concat(BUCKETS).forEach(function (b) {
      var n = res.counts[b];
      if (b !== "all" && b !== role && !n) return;
      chips.appendChild(chip(ROLE_TITLES[b], role === b, function () {
        go(["performers", b], { with: query.with, q: query.q });
      }, n));
    });
    wrap.appendChild(chips);
    var search = h("input", { type: "search", class: "input", placeholder: "Find a performer", value: query.q || "",
      "aria-label": "Find a performer", autocomplete: "off",
      oninput: debounce(function () { go(["performers", role], { with: query.with, q: search.value.trim() }, true); },
        350) });
    wrap.appendChild(h("div", { class: "filters" }, search));
    if (res.with) {
      wrap.appendChild(h("div", { class: "scope" },
        h("span", { text: "Only those who recorded with " + res.with.name }),
        h("button", { type: "button", class: "chip on", text: "Clear ✕",
          onclick: function () { go(["performers", role], { q: query.q }); } })));
    }
    var list = h("div", { class: "list" });
    res.items.forEach(function (p) {
      var what = (p.instruments && p.instruments.length ? p.instruments : p.roles.map(function (r) {
        return (BUCKET_TITLES[r] || r).replace(/s$/, "").toLowerCase();
      })).join(", ");
      list.appendChild(h("a", { class: "row", href: formatHash(["performer", p.id], { with: res.with && res.with.id }) },
        h("span", { class: "row-main" },
          h("span", { class: "row-title", text: p.name }),
          h("span", { class: "row-sub", text: [what, plural(p.recordings, "recording"), plural(p.works, "work")]
            .filter(Boolean).join(" · ") })),
        h("span", { class: "chev", "aria-hidden": "true", text: "›" })));
    });
    if (!res.items.length) list.appendChild(empty("No performers here."));
    wrap.appendChild(list);
    return wrap;
  };

  VIEWS.performer = async function (args, query) {
    var pid = args[0];
    var res = await call("classical/performer", { id: pid, with_id: query.with || null });
    var p = res.performer;
    var what = (p.instruments && p.instruments.length ? p.instruments : p.roles.map(function (r) {
      return (BUCKET_TITLES[r] || r).replace(/s$/, "").toLowerCase();
    })).join(", ");
    var wrap = h("div", { class: "page" }, header(p.name, [what, plural(p.recordings, "recording")]
      .filter(Boolean).join(" · "), { href: "#/performers", label: "Performers" }));
    if (res.with) {
      wrap.appendChild(h("div", { class: "scope" },
        h("span", { text: "With " + res.with.name }),
        h("button", { type: "button", class: "chip on", text: "Clear ✕",
          onclick: function () { go(["performer", pid], {}); } })));
    }
    var partners = res.partners || {};
    var pb = BUCKETS.filter(function (b) { return partners[b] && partners[b].length; });
    if (pb.length && !res.with) {
      var sec = h("section", { class: "section" }, h("h2", { text: "Recorded with" }));
      pb.forEach(function (b) {
        var chips = h("div", { class: "chips scroll" });
        partners[b].slice(0, 30).forEach(function (x) {
          chips.appendChild(chip(x.name, false, function () { go(["performer", pid], { with: x.id }); }, x.n));
        });
        sec.appendChild(h("h3", { class: "mini", text: BUCKET_TITLES[b] }));
        sec.appendChild(chips);
      });
      wrap.appendChild(sec);
    }
    var scope = res.with ? pid + "," + res.with.id : pid;
    var list = h("div", { class: "list" });
    res.works.forEach(function (w) { list.appendChild(workRow(w, scope)); });
    if (!res.works.length) list.appendChild(empty("No works."));
    wrap.appendChild(h("section", { class: "section" }, h("h2", { text: "Works" }), list));
    return wrap;
  };

  // ---- review queue ---------------------------------------------------------------------------------------

  VIEWS.review = async function (args, query) {
    var canCorrect = (state.info || {}).can_correct;
    var res = await call("classical/review_triage", reviewArgs(query));
    var dismissed = query.status === "dismissed";
    var filtered = REVIEW_KEYS.some(function (k) { return k !== "sort" && k !== "status" && query[k]; });
    var wrap = h("div", { class: "page" }, header("Review", res.total
      ? plural(res.items, "item") + " in " + plural(res.cards_total, "decision") +
        (res.items !== res.total ? " (of " + res.total + ")" : "") +
        (res.dismissed ? " · " + res.dismissed + " hidden" : "")
      : res.dismissed ? "Nothing open · " + res.dismissed + " hidden" : "Nothing to review"));
    function update(patch) { go(["review"], reviewQuery(query, patch), true); }

    var chips = h("div", { class: "chips scroll" });
    chips.appendChild(chip("All", !query.kind, function () { update({ kind: "" }); }, res.items));
    res.kinds.forEach(function (k) {
      if (!k.items && query.kind !== k.kind) return;
      chips.appendChild(chip(k.title, query.kind === k.kind, function () { update({ kind: k.kind }); }, k.items));
    });
    wrap.appendChild(chips);

    var search = h("input", { type: "search", class: "input", placeholder: "Title, album or credit",
      value: query.q || "", "aria-label": "Search review", autocomplete: "off",
      oninput: debounce(function () { update({ q: search.value.trim() }); }, 350) });
    function facet(label, all, key, items, name) {
      var sel = h("select", { class: "input", "aria-label": label, onchange: function () {
        var p = {}; p[key] = sel.value; update(p);
      } }, h("option", { value: "", text: all }));
      var seen = false;
      items.forEach(function (x) {
        var on = query[key] === String(x.id);
        seen = seen || on;
        sel.appendChild(h("option", { value: x.id, text: name(x) + " (" + x.n + ")", selected: on ? true : null }));
      });
      if (query[key] && !seen) {
        sel.appendChild(h("option", { value: query[key], text: "Current choice", selected: true }));
      }
      return sel;
    }
    var row1 = h("div", { class: "filter-grid" },
      facet("Composer", "All composers", "composer", res.composers, function (x) { return x.name; }),
      facet("Album", "All albums", "album", res.albums, function (x) { return x.name; }));
    var sortSel = h("select", { class: "input", "aria-label": "Sort decisions", onchange: function () {
      update({ sort: sortSel.value });
    } });
    [["impact", "Most tracks affected"], ["acclaim", "Most acclaimed"], ["items", "Most items"], ["title", "Title"]]
      .forEach(function (o) {
        sortSel.appendChild(h("option", { value: o[0], text: o[1], selected: res.sort === o[0] ? true : null }));
      });
    var row2 = h("div", { class: "filter-grid" }, sortSel);
    var reasons = (res.reasons || []).filter(function (r) { return r.n || r.reason === query.reason; });
    if (reasons.length > 1 || query.reason) {
      row2.insertBefore(facet("Reason", "All reasons", "reason", reasons.map(function (r) {
        return { id: r.reason, name: r.title, n: r.n };
      }), function (x) { return x.name; }), sortSel);
    }
    wrap.appendChild(h("div", { class: "filters" }, search, row1, row2));

    var bar = h("div", { class: "actions review-bar" });
    if (filtered) {
      bar.appendChild(h("button", { type: "button", class: "chip on", text: "Clear filters ✕", onclick: function () {
        go(["review"], reviewQuery({}, { status: query.status, sort: query.sort }));
      } }));
    }
    if (res.dismissed || dismissed) {
      bar.appendChild(chip(dismissed ? "Showing hidden" : "Hidden", dismissed, function () {
        update({ status: dismissed ? "" : "dismissed" });
      }, res.dismissed));
    }
    if (canCorrect && !dismissed && filtered && res.items > 1) {
      bar.appendChild(armed("Hide all shown (" + res.items + ")", "btn small", async function () {
        var f = {};
        ["kind", "reason", "composer", "album", "q"].forEach(function (k) { if (query[k]) f[k] = query[k]; });
        await decide([], f);
      }));
    }
    if (bar.childNodes.length) wrap.appendChild(bar);

    var list = h("div", { class: "cards" });
    res.cards.forEach(function (c) { list.appendChild(reviewCard(c, canCorrect)); });
    if (!res.cards.length) {
      list.appendChild(empty(dismissed ? "Nothing hidden here." : filtered ? "Nothing matches these filters."
        : "Every album was grouped with confidence."));
    }
    wrap.appendChild(list);
    var next = res.next;
    var more = h("button", { type: "button", class: "btn wide", text: "Show more", hidden: next ? null : true,
      onclick: async function () {
        more.disabled = true;
        try {
          var page = await call("classical/review_triage", reviewArgs(query, next));
          page.cards.forEach(function (c) { list.appendChild(reviewCard(c, canCorrect)); });
          next = page.next;
          more.hidden = !next;
        } catch (e) { toast(e.message); }
        more.disabled = false;
      } });
    wrap.appendChild(more);
    if (canCorrect) wrap.appendChild(recentDecisions());
    return wrap;
  };

  /** A button that runs one request at a time; with `confirm`, it asks for a second, separate tap first. */
  function guarded(label, cls, run, confirm) {
    var gate = tapGate(confirm), timer = null;
    var b = h("button", { type: "button", class: cls, text: label, onclick: async function () {
      var t = gate.tap(Date.now());
      if (t === "arm") {
        b.textContent = label + "? Tap again";
        clearTimeout(timer);
        timer = setTimeout(function () { gate.disarm(); b.textContent = label; }, 5000);
        return;
      }
      if (t !== "run") return;
      clearTimeout(timer);
      b.disabled = true;
      b.textContent = label;
      try { await run(); } finally { gate.done(); b.disabled = false; }
    } });
    return b;
  }

  /** A button that asks once more before doing something to many items. */
  function armed(label, cls, run) { return guarded(label, cls, run, true); }

  function refresh() { state.keep = true; render(); }

  /** One undoable review decision; the toast offers Undo. */
  async function decide(actions, dismissFilter) {
    try {
      var args = { actions: actions };
      if (dismissFilter) args.dismiss_filter = dismissFilter;
      var res = await call("classical/review_apply", args);
      undoToast(res.batch ? res.label : "Nothing changed", res.batch);
      refresh();
    } catch (e) { toast(e.message); }
  }

  function undoToast(text, batch) {
    toast(text, batch ? { label: "Undo", run: function () { undoBatch(batch); } } : null);
  }

  async function undoBatch(batch) {
    try {
      await call("classical/review_undo", { batch: batch });
      toast("Undone");
      refresh();
    } catch (e) { toast(e.message); }
  }

  function recentDecisions() {
    var sec = h("section", { class: "section" }, h("h2", { text: "Your recent decisions" }));
    var list = h("div", { class: "list" });
    var opener = h("button", { type: "button", class: "btn wide", text: "Show recent decisions",
      onclick: async function () {
        opener.disabled = true;
        try {
          var res = await call("classical/review_history", { limit: 30 });
          opener.remove();
          var now = Date.now() / 1000;
          res.items.forEach(function (b) {
            list.appendChild(h("div", { class: "row" },
              h("span", { class: "row-main" },
                h("span", { class: "row-title", text: b.label || b.op }),
                h("span", { class: "row-sub", text: historyLine(b, now) })),
              b.can_undo ? guarded("Undo", "btn small", function () { return undoBatch(b.batch); }) : null));
          });
          if (!res.items.length) list.appendChild(empty("No decisions yet."));
        } catch (e) { opener.disabled = false; toast(e.message); }
      } });
    sec.appendChild(opener);
    sec.appendChild(list);
    return sec;
  }

  var CARD_PREVIEW = 3;

  function reviewCard(c, canCorrect) {
    var card = h("section", { class: "review-card" + (c.actionable ? "" : " note") },
      h("div", { class: "review-card-head" },
        h("span", { class: "kind", text: KIND_LABELS[c.kind] || c.kind }),
        h("h3", { text: c.title }),
        c.subtitle && c.subtitle !== c.title ? h("p", { class: "row-sub", text: c.subtitle }) : null,
        h("p", { class: "fine", text: cardMeta(c) })));
    if (c.hint) card.appendChild(h("p", { class: "fine", text: c.hint }));
    var acts = h("div", { class: "actions" });
    (c.group_actions || []).forEach(function (a) { acts.appendChild(actionButton(a, true)); });
    if (acts.childNodes.length) card.appendChild(acts);
    if (!canCorrect && c.actionable) card.appendChild(h("p", { class: "fine", text: "An administrator can decide this." }));
    var list = h("div", { class: "list" });
    var rows = c.kind === "name" ? albumRows(c.items).map(function (r) {
      return function () { return creditRow(r); };
    }) : c.items.map(function (it) { return function () { return reviewItem(it); }; });
    var label = "Show all " + rows.length + (c.kind === "name" ? " albums" : "");
    var open = false;
    function draw() {
      clear(list);
      (open ? rows : rows.slice(0, CARD_PREVIEW)).forEach(function (make) { list.appendChild(make()); });
    }
    draw();
    card.appendChild(list);
    if (rows.length > CARD_PREVIEW) {
      var toggle = h("button", { type: "button", class: "link-btn", text: label, onclick: function () {
        open = !open;
        draw();
        toggle.textContent = open ? "Show fewer" : label;
      } });
      card.appendChild(toggle);
    }
    if (c.more) card.appendChild(h("p", { class: "fine", text: "and " + c.more + " more: narrow with the filters" }));
    return card;
  }

  var KIND_LABELS = { name: "Composer credit", mb: "MusicBrainz", accolade: "Award", discs: "Discs",
    same_work: "Same work?", info: "Note" };

  /** A server-built action: an undoable batch (hide, reject, link, join), or a credit mapping (a sheet). */
  function actionButton(a, group) {
    if (a.type === "composer") {
      return h("button", { type: "button", class: "btn small" + (group ? " primary" : ""), text: a.label,
        onclick: function () { composerNameSheet(a); } });
    }
    var cls = "btn small" + (a.accept && !group ? " primary" : "");
    var n = actionCount(a.actions);
    var run = function () { return decide(a.actions); };
    return guarded(a.label, cls, run, n > 1);
  }

  /** One album under a credit card: where the credit appears, and its per-album exception. */
  function creditRow(r) {
    var it = r.item;
    var box = h("div", { class: "review-item" });
    box.appendChild(h("div", { class: "row" }, h("span", { class: "row-main" },
      h("span", { class: "row-title", text: r.title }),
      r.others.length ? h("span", { class: "row-sub", text: "Could be " + r.others.join(" or ") }) : null)));
    var acts = h("div", { class: "actions" });
    (it.actions || []).forEach(function (a) { if (a.type === "composer") acts.appendChild(actionButton(a, false)); });
    if (acts.childNodes.length) box.appendChild(acts);
    return box;
  }

  function reviewLink(l) {
    if (!l) return null;
    if (l.kind === "work") return formatHash(["work", l.id]);
    if (l.kind === "recording") return formatHash(["recording", l.id]);
    if (l.kind === "composer") return formatHash(["composer", l.id]);
    return null;
  }

  function reviewItem(it) {
    var sub = reviewSubtitle(it);
    var href = reviewLink(it.link);
    var box = h("div", { class: "review-item" });
    box.appendChild(h(href ? "a" : "div", { class: "row", href: href },
      h("span", { class: "row-main" },
        h("span", { class: "row-title", text: it.title || it.entity }),
        sub ? h("span", { class: "row-sub", text: sub }) : null),
      href ? h("span", { class: "chev", "aria-hidden": "true", text: "›" }) : null));
    var tracks = it.detail && it.detail.tracks;
    if (tracks && tracks.length) {
      var ul = h("ul", { class: "detail" });
      tracks.forEach(function (t) { ul.appendChild(h("li", { text: t })); });
      box.appendChild(ul);
    }
    if ((it.reason === "accolade_match" || it.reason === "possible continuation on another disc") &&
        it.detail && it.detail.why && it.detail.why !== sub) {
      box.appendChild(h("p", { class: "fine", text: it.detail.why }));
    }
    if (it.reason === "Link to MusicBrainz work?") {
      var cl = h("ul", { class: "detail" });
      mbWorkCandidates(it).forEach(function (c) {
        var label = c.title + (c.why ? " — " + c.why : "");
        cl.appendChild(h("li", null, c.href ? h("a", { href: c.href, target: "_blank", rel: "noopener noreferrer",
          text: label }) : h("span", { text: label })));
      });
      if (cl.childNodes.length) box.appendChild(cl);
    }
    var acts = h("div", { class: "actions" });
    (it.actions || []).forEach(function (a) { acts.appendChild(actionButton(a, false)); });
    if (acts.childNodes.length) box.appendChild(acts);
    if (it.hint) box.appendChild(h("p", { class: "fine", text: it.hint }));
    return box;
  }

  function composerNameSheet(a) {
    var chosen = null;
    var list = h("div", { class: "list" });
    var saving = false;
    var confirm = h("button", { type: "button", class: "btn primary", text: "Save", disabled: true,
      onclick: async function () {
        if (!chosen || saving) return;
        saving = true;
        try {
          var args = { name: a.name, composer_id: chosen.id };
          if (a.album_id) args.album_id = a.album_id;
          var res = await call("classical/composer_name", args);
          close();
          undoToast("Saved: “" + a.name + "” is " + chosen.name + (a.album_id ? " on this album" : ""), res.batch);
          refresh();
        } catch (e) { toast(e.message); }
        saving = false;
      } });
    function option(c) {
      return h("button", { type: "button", class: "row row-btn", onclick: function (e) {
        chosen = c;
        Array.prototype.forEach.call(list.querySelectorAll(".row-btn"), function (b) { b.classList.remove("on"); });
        e.currentTarget.classList.add("on");
        confirm.disabled = false;
        confirm.textContent = "Save: " + c.name;
      } }, h("span", { class: "row-main" }, h("span", { class: "row-title", text: c.name }),
        c.born || c.died ? h("span", { class: "row-sub", text: lifeDates(c.born, c.died) }) : null));
    }
    (a.suggestions || []).forEach(function (c) { list.appendChild(option(c)); });
    var all = null;
    var search = h("input", { type: "search", class: "input", placeholder: "Search all composers",
      "aria-label": "Search all composers", autocomplete: "off",
      oninput: debounce(async function () {
        var ws = words(search.value);
        if (!all) {
          try { all = (await call("classical/composers")).items; } catch (e) { toast(e.message); return; }
        }
        clear(list);
        var pool = ws.length ? all.filter(function (c) {
          var t = fold(c.name + " " + c.sort_name);
          return ws.every(function (x) { return t.indexOf(x) >= 0; });
        }).slice(0, 40) : (a.suggestions || []);
        pool.forEach(function (c) { list.appendChild(option(c)); });
      }, 250) });
    var body = h("div", null,
      h("p", { text: "Which composer does the credit “" + a.name + "” mean" +
        (a.album_id ? " on this album? Other albums keep the general choice." : "? Every album with this credit follows.") }),
      search, list,
      h("div", { class: "actions" }, confirm,
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Composer credit", body);
  }

  // ---- boot -----------------------------------------------------------------------------------------------

  function renderTop() {
    var top = clear($("top"));
    var info = state.info || {};
    var user = info.user || {};
    top.appendChild(h("a", { class: "brand", href: "#/composers" },
      h("img", { src: "icon.svg", alt: "", class: "brand-icon" }), h("span", { text: "Classical" })));
    top.appendChild(h("button", { type: "button", class: "user-btn", "aria-label": "Account",
      text: (user.display_name || user.username || "Me").charAt(0).toUpperCase(),
      onclick: function () {
        var body = h("div", null,
          h("p", { text: user.display_name || user.username || "Signed in" }),
          h("p", { class: "fine", text: state.source === "ingress" ? "Signed in through Home Assistant."
            : state.source === "borrowed" ? "Using your Music Assistant web sign-in." : "Signed in on this device." }),
          h("p", { class: "fine", text: "Classical " + (info.version || "") }),
          state.source === "ingress" ? null : h("button", { type: "button", class: "btn", text: "Sign out",
            onclick: function () { close(); signOut(); } }));
        var close = sheet("Account", body);
      } }));
  }

  async function boot() {
    if (!(await authenticate())) { showLogin(); return; }
    $("login").hidden = true;
    $("app").hidden = false;
    try { state.info = await call("classical/ui_info"); } catch (e) { state.info = {}; }
    renderTop();
    window.addEventListener("hashchange", render);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") stopPolling(); else startPolling();
    });
    render();
    await loadPlayers();
    startPolling();
  }

  boot();
})();
