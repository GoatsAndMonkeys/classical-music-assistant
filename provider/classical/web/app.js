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

  function workSubtitle(w) {
    return [w.composer, w.catalogue, w.key, w.work_type].filter(function (x, i, a) {
      return x && a.indexOf(x) === i && !(i > 0 && w.title && x !== w.work_type && w.title.indexOf(x) >= 0);
    }).join(" · ");
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

  var pure = {
    BUCKETS: BUCKETS, parseHash: parseHash, formatHash: formatHash, takeCode: takeCode, fmtDuration: fmtDuration,
    lifeDates: lifeDates, yearText: yearText, workSubtitle: workSubtitle, recordingMatches: recordingMatches,
    filterRecordings: filterRecordings, sortRecordings: sortRecordings, creditGroups: creditGroups,
    reviewSubtitle: reviewSubtitle, badgeText: badgeText, anyAcclaim: anyAcclaim, safeHref: safeHref,
    byLetter: byLetter, playArgs: playArgs, loginReturnUrl: loginReturnUrl, choosePlayer: choosePlayer, errorText: errorText, imageSrc: imageSrc,
    plural: plural, pickLines: pickLines, daySubtitle: daySubtitle, localDate: localDate, probeOrder: probeOrder,
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
    performers: "performers", performer: "performers", review: "review" };
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
    return wrap;
  }

  function workRow(w, scope, noComposer) {
    var sub = noComposer ? Object.assign({}, w, { composer: null }) : w;
    var count = w.mine !== undefined && w.mine !== w.recordings
      ? w.mine + " of " + plural(w.recordings, "recording") : plural(w.recordings, "recording");
    return h("a", { class: "row", href: formatHash(["work", w.id], { scope: scope }) },
      h("span", { class: "row-main" },
        h("span", { class: "row-title", text: w.title }),
        h("span", { class: "row-sub", text: [workSubtitle(sub), count].filter(Boolean).join(" · ") })),
      h("span", { class: "chev", "aria-hidden": "true", text: "›" }));
  }

  // ---- works ----------------------------------------------------------------------------------------------

  VIEWS.works = async function (args, query) {
    var f = { composer_id: query.composer || null, work_type: query.type || null, q: query.q || null,
      date_kind: query.dk || "composed", date_from: num(query.from), date_to: num(query.to),
      performer_id: query.performer || null };
    var res = await call("classical/works", Object.assign({}, f, { limit: 200 }));
    var wrap = h("div", { class: "page" }, header("Works", plural(res.total, "work")));

    function update(patch) {
      var q = Object.assign({ composer: query.composer, type: query.type, q: query.q, dk: query.dk,
        from: query.from, to: query.to, performer: query.performer }, patch);
      if (q.dk === "composed") q.dk = "";
      go(["works"], q, true);
    }

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
    [["composed", "Composed"], ["recorded", "Recorded"], ["released", "Released"]].forEach(function (o) {
      dk.appendChild(h("option", { value: o[0], text: o[1], selected: f.date_kind === o[0] ? true : null }));
    });
    var from = h("input", { class: "input year", type: "text", inputmode: "numeric", pattern: "[0-9]*", maxlength: "4", placeholder: "From",
      value: query.from || "", "aria-label": "From year", min: "800", max: "2100" });
    var to = h("input", { class: "input year", type: "text", inputmode: "numeric", pattern: "[0-9]*", maxlength: "4", placeholder: "To",
      value: query.to || "", "aria-label": "To year", min: "800", max: "2100" });
    var years = debounce(function () { update({ from: from.value, to: to.value }); }, 600);
    from.addEventListener("input", years);
    to.addEventListener("input", years);

    var cov = res.date_coverage || {};
    var covText = f.date_kind === "composed"
      ? (cov.composed || 0) + " of " + plural(cov.total || 0, "work") + " have a composition date"
      : null;

    wrap.appendChild(h("div", { class: "filters" },
      search,
      h("div", { class: "filter-row" }, typeSel),
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
      hits.slice(0, shown).forEach(function (r) { list.appendChild(recRow(r, expanded)); });
      if (!hits.length) list.appendChild(empty("No recordings match."));
      moreBtn.hidden = hits.length <= shown;
    }
    draw();
    return wrap;
  };

  function recRow(r, expanded) {
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
      h("span", { class: "row-sub", text: [yearText(r), r.album, r.tracks ? plural(r.tracks, "track") : null]
        .filter(Boolean).join(" · ") }),
      badges(r.accolades, 2)),
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
        badges(r.accolades))));
    wrap.appendChild(recordingBody(r, false));
    wrap.appendChild(accoladesSection(r));
    if (r.merged && (state.info || {}).can_correct) {
      wrap.appendChild(h("section", { class: "section" },
        h("p", { class: "fine", text: "This album's " + (w.title || "work") +
          " was matched to the same work on other albums." }),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn", text: "Keep separate",
          onclick: function () { separateSheet(r, w); } }))));
    }
    return wrap;
  };

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

  VIEWS.review = async function () {
    var res = await call("classical/review");
    var info = state.info || {};
    var wrap = h("div", { class: "page" }, header("Review", res.total
      ? plural(res.total, "item") + " the index wasn't sure about" : "Nothing to review"));
    if (!res.total) {
      wrap.appendChild(empty("Every album was grouped with confidence."));
      return wrap;
    }
    res.groups.forEach(function (g) {
      var list = h("div", { class: "list" });
      g.items.forEach(function (it) { list.appendChild(reviewItem(it, info.can_correct)); });
      wrap.appendChild(h("section", { class: "section" },
        h("h2", null, reasonTitle(g.reason), h("span", { class: "chip-n", text: String(g.n) })), list));
    });
    return wrap;
  };

  function reasonTitle(reason) {
    if (reason === "accolade_match") return "Possible award: is this the awarded recording?";
    var s = String(reason || "").replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function reviewLink(l) {
    if (!l) return null;
    if (l.kind === "work") return formatHash(["work", l.id]);
    if (l.kind === "recording") return formatHash(["recording", l.id]);
    if (l.kind === "composer") return formatHash(["composer", l.id]);
    return null;
  }

  function reviewItem(it, canCorrect) {
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
    if (it.reason === "accolade_match" && it.detail && it.detail.why) {
      box.appendChild(h("p", { class: "fine", text: it.detail.why }));
    }
    var acts = h("div", { class: "actions" });
    (it.actions || []).forEach(function (a) {
      if (!canCorrect) return;
      if (a.op === "composer_name") {
        acts.appendChild(h("button", { type: "button", class: "btn", text: "This credit means…",
          onclick: function () { composerNameSheet(a); } }));
      } else if (a.op === "accolade_decide") {
        acts.appendChild(h("button", { type: "button", class: "btn", text: a.label, onclick: async function () {
          try {
            await call("classical/accolade_decide", { entry_ref: a.entry, recording_id: a.recording_id,
              decision: a.decision });
            toast(a.decision === "confirm" ? "Award shown on this recording" : "Award hidden for this recording");
            render();
          } catch (e) { toast(e.message); }
        } }));
      } else if (a.op === "merge_composers") {
        acts.appendChild(h("button", { type: "button", class: "btn",
          text: "Merge " + a.source.name + " into " + a.target.name,
          onclick: function () { mergeSheet(a); } }));
      }
    });
    if (acts.childNodes.length) box.appendChild(acts);
    if (it.actions && it.actions.length && !canCorrect) {
      box.appendChild(h("p", { class: "fine", text: "An administrator can correct this." }));
    }
    if (it.hint) box.appendChild(h("p", { class: "fine", text: it.hint }));
    box.appendChild(h("p", { class: "fine mono", text: it.id }));
    return box;
  }

  function mergeSheet(a) {
    var body = h("div", null,
      h("p", { text: "All works by " + a.source.name + " will be credited to " + a.target.name +
        ". Future scans keep this choice." }),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", text: "Merge", onclick: async function () {
          try {
            await call("classical/merge_composers", { source_id: a.source.id, target_id: a.target.id });
            close();
            toast("Merged");
            render();
          } catch (e) { toast(e.message); }
        } }),
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { close(); } })));
    var close = sheet("Merge composers?", body);
  }

  function composerNameSheet(a) {
    var chosen = null;
    var list = h("div", { class: "list" });
    var confirm = h("button", { type: "button", class: "btn primary", text: "Save", disabled: true,
      onclick: async function () {
        if (!chosen) return;
        try {
          await call("classical/composer_name", { name: a.name, composer_id: chosen.id });
          close();
          toast("Saved: “" + a.name + "” is " + chosen.name);
          render();
        } catch (e) { toast(e.message); }
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
      h("p", { text: "Which composer does the credit “" + a.name + "” mean?" }),
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
