"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- preferences (this phone only) ----------
  const PREFS_KEY = "remote.prefs";
  const prefs = Object.assign({ sound: true, volume: 0.8, doneChime: true, nag: true, wake: true }, (() => {
    try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch (e) { return {}; }
  })());
  const savePrefs = () => { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {} };

  async function api(path, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Remote": "1" }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.error || "Request failed (" + r.status + ")");
    return data;
  }

  let toastTimer;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ---------- clock ----------
  const dateFmt = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });
  function tick() {
    const d = new Date();
    $("hm").textContent = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    $("sec").textContent = String(d.getSeconds()).padStart(2, "0");
    $("date").textContent = dateFmt.format(d);
  }
  tick();
  setInterval(tick, 1000);

  // ---------- sound (Web Audio, unlocked by the first tap) ----------
  let ctx = null;
  function audio() {
    if (!ctx) { const A = window.AudioContext || window.webkitAudioContext; if (A) ctx = new A(); }
    if (ctx && ctx.state === "suspended") ctx.resume();
    return ctx;
  }
  function tone(freq, start, dur, type, gain) {
    const c = audio(); if (!c) return;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type || "sine"; o.frequency.value = freq;
    const t0 = c.currentTime + start, peak = (gain || 0.3) * prefs.volume;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(c.destination);
    o.start(t0); o.stop(t0 + dur + 0.05);
  }
  const SOUNDS = {
    permission() { [0, 0.16, 0.32].forEach((s, i) => tone([784, 988, 1319][i], s, 0.35, "triangle", 0.35)); tone(1319, 0.7, 0.5, "triangle", 0.3); tone(1568, 0.7, 0.5, "sine", 0.15); },
    attention() { tone(880, 0, 0.3, "triangle", 0.3); tone(1175, 0.18, 0.45, "triangle", 0.3); },
    done() { tone(1047, 0, 0.9, "sine", 0.22); tone(1568, 0.06, 0.8, "sine", 0.08); },
  };
  function play(kind) {
    if (!prefs.sound) return;
    if (kind === "done" && !prefs.doneChime) return;
    if (SOUNDS[kind]) SOUNDS[kind]();
    if (navigator.vibrate && kind !== "done") navigator.vibrate(kind === "permission" ? [120, 80, 120] : 100);
  }

  // ---------- keep the screen on ----------
  let wakeLock = null;
  async function keepAwake() {
    if (!prefs.wake || !("wakeLock" in navigator) || document.visibilityState !== "visible") return;
    try { wakeLock = await navigator.wakeLock.request("screen"); } catch (e) {}
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { keepAwake(); pollSpotify(); } });

  // ---------- start overlay ----------
  $("start").addEventListener("click", () => {
    audio();
    tone(660, 0, 0.2, "sine", 0.15);
    const el = document.documentElement;
    if (el.requestFullscreen && !document.fullscreenElement) el.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    keepAwake();
    $("start").hidden = true;
  });

  // ---------- Claude ----------
  let snap = null, lastAlert = null, askIndex = 0, offsetMs = 0;

  function ago(ts) {
    const s = Math.max(0, Math.round((Date.now() + offsetMs) / 1000 - ts));
    if (s < 45) return "now";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return Math.round(s / 3600) + "h";
    return Math.round(s / 86400) + "d";
  }
  const STATE_TEXT = { working: "Working", permission: "Needs approval", waiting: "Needs you", idle: "Idle", ended: "Ended" };

  function renderSessions() {
    const list = $("sessions");
    const sessions = (snap && snap.sessions) || [];
    $("empty").hidden = sessions.length > 0;
    list.hidden = sessions.length === 0;
    const live = sessions.filter((s) => s.state !== "ended").length;
    const busy = sessions.filter((s) => s.state === "permission" || s.state === "waiting").length;
    $("count").textContent = sessions.length ? (busy ? busy + " waiting" : live + " active") : "";
    list.innerHTML = sessions.map((s) => `
      <li class="sess ${esc(s.state)}" data-id="${esc(s.id)}">
        <span class="dot"></span>
        <span class="name">${esc(s.name || "Session")}</span>
        <span class="when">${esc(ago(s.updated))}</span>
        <span class="act">${esc(s.activity || STATE_TEXT[s.state] || "")}</span>
        ${s.state === "ended" || s.state === "idle" ? `<button class="x" data-dismiss="${esc(s.id)}" title="Remove"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>` : `<span></span>`}
        ${s.prompt ? `<span class="prompt">“${esc(s.prompt)}”</span>` : ""}
      </li>`).join("");
  }

  $("sessions").addEventListener("click", async (e) => {
    const x = e.target.closest("[data-dismiss]");
    if (x) { e.stopPropagation(); try { await api("/api/sessions/" + encodeURIComponent(x.dataset.dismiss) + "/dismiss", {}); } catch (err) { toast(err.message); } return; }
    const li = e.target.closest(".sess.permission");
    if (li && snap) {
      const i = snap.pending.findIndex((p) => p.session_id === li.dataset.id);
      if (i >= 0) { askIndex = i; askHidden = false; renderAsk(); }
    }
  });

  let askHidden = false, shownAskId = null;
  function renderAsk() {
    const pending = (snap && snap.pending) || [];
    const ask = $("ask");
    if (!pending.length || askHidden) { ask.hidden = true; shownAskId = null; return; }
    if (askIndex >= pending.length) askIndex = 0;
    const p = pending[askIndex];
    if (shownAskId !== p.id) {
      shownAskId = p.id;
      $("askSession").textContent = p.session || "Claude";
      $("askTitle").textContent = p.title;
      $("askTool").textContent = p.tool;
      $("askDetail").textContent = p.detail || "";
      $("askDetail").scrollTop = 0;
      $("allowBtn").disabled = $("denyBtn").disabled = false;
    }
    $("askOf").textContent = pending.length > 1 ? (askIndex + 1) + " of " + pending.length : "";
    ask.hidden = false;
    updateTimer();
  }
  function updateTimer() {
    if (!snap || $("ask").hidden) return;
    const p = snap.pending[askIndex];
    if (!p) return;
    const left = Math.max(0, Math.round(p.created + (snap.settings.wait || 600) - (Date.now() + offsetMs) / 1000));
    $("askTimer").textContent = left > 0 ? Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") : "";
  }
  setInterval(() => { updateTimer(); if (snap) renderSessionsTimes(); }, 1000);
  function renderSessionsTimes() {
    document.querySelectorAll(".sess").forEach((li) => {
      const s = snap.sessions.find((x) => x.id === li.dataset.id);
      if (s) li.querySelector(".when").textContent = ago(s.updated);
    });
  }

  async function decide(decision) {
    const p = snap && snap.pending[askIndex];
    if (!p) return;
    $("allowBtn").disabled = $("denyBtn").disabled = true;
    try {
      await api("/api/requests/" + encodeURIComponent(p.id), { decision });
      if (navigator.vibrate) navigator.vibrate(30);
    } catch (e) {
      toast(e.message);
      $("allowBtn").disabled = $("denyBtn").disabled = false;
    }
  }
  $("allowBtn").addEventListener("click", () => decide("allow"));
  $("denyBtn").addEventListener("click", () => decide("deny"));
  $("ask").addEventListener("click", (e) => { if (e.target === $("ask")) { askHidden = true; renderAsk(); } });

  function renderMode() {
    const away = !!(snap && snap.settings.away);
    $("mode").setAttribute("aria-pressed", String(away));
    $("modeLbl").textContent = away ? "Phone answers" : "At desk";
  }
  $("mode").addEventListener("click", async () => {
    const away = !(snap && snap.settings.away);
    try {
      await api("/api/settings", { away });
      toast(away ? "Permission prompts come to the phone" : "Permission prompts stay on the computer");
    } catch (e) { toast(e.message); }
  });

  function apply(next) {
    const prevPending = new Set(((snap && snap.pending) || []).map((p) => p.id));
    snap = next;
    offsetMs = next.now * 1000 - Date.now();
    if (next.pending.some((p) => !prevPending.has(p.id))) { askHidden = false; askIndex = next.pending.length - 1; }
    if (lastAlert !== null && next.alert.seq !== lastAlert) play(next.alert.kind);
    lastAlert = next.alert.seq;
    renderMode();
    renderSessions();
    renderAsk();
  }

  // reminder while something waits
  setInterval(() => {
    if (!prefs.nag || !snap) return;
    if (snap.pending.length) play("permission");
    else if (snap.sessions.some((s) => s.state === "waiting")) play("attention");
  }, 45000);

  let es = null, offlineTimer = null;
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/stream");
    es.onmessage = (e) => {
      clearTimeout(offlineTimer); $("offline").hidden = true;
      try { apply(JSON.parse(e.data)); } catch (err) {}
    };
    es.onerror = () => {
      clearTimeout(offlineTimer);
      offlineTimer = setTimeout(() => { $("offline").hidden = false; }, 2500);
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
    };
  }
  connect();

  // ---------- Spotify ----------
  let sp = null, spAt = 0, spBusy = false, volTimer = null, volDragging = false;
  const PLAY = '<path d="M7 4v16l13-8z" class="fill"/>';
  const PAUSE = '<rect x="6" y="4" width="4" height="16" rx="1" class="fill"/><rect x="14" y="4" width="4" height="16" rx="1" class="fill"/>';

  function renderSpotify() {
    const off = $("musicOff");
    if (!sp || !sp.connected) {
      off.hidden = false;
      off.innerHTML = '<div><b>Spotify not connected</b><a class="chip" href="/setup">Set up Spotify</a></div>';
      return;
    }
    off.hidden = true;
    const active = sp.active && sp.title;
    $("trTitle").textContent = active ? sp.title : "Nothing playing";
    $("trArtist").textContent = active ? sp.artist : "Pick a device or playlist";
    if (sp.image) {
      if ($("art").getAttribute("src") !== sp.image) { $("art").src = sp.image; $("artBg").style.backgroundImage = 'url("' + sp.image.replace(/"/g, "") + '")'; }
      $("art").hidden = false; $("artEmpty").hidden = true;
    } else {
      $("art").hidden = true; $("artEmpty").hidden = false; $("artBg").style.backgroundImage = "none";
    }
    $("playIcon").innerHTML = sp.playing ? PAUSE : PLAY;
    $("shuffle").classList.toggle("on", !!sp.shuffle);
    $("repeat").classList.toggle("on", sp.repeat && sp.repeat !== "off");
    $("repeat").classList.toggle("one", sp.repeat === "track");
    $("devName").textContent = (sp.device && sp.device.name) || "Pick";
    $("onDev").textContent = sp.device && sp.device.name ? "on " + sp.device.name : "";
    if (!volDragging && sp.device && sp.device.volume != null) setVol(sp.device.volume);
    $("vol").disabled = !(sp.device && sp.device.can_volume !== false);
    renderProgress();
    $("lyrBtn").disabled = !(sp.active && sp.title);
    if (lyr.open) loadLyrics();
  }
  function setVol(v) { $("vol").value = v; $("vol").style.setProperty("--p", v + "%"); }
  function renderProgress() {
    if (!sp || !sp.duration) { $("bar").style.width = "0"; return; }
    const pos = Math.min(sp.duration, sp.progress + (sp.playing ? Date.now() - spAt : 0));
    $("bar").style.width = (100 * pos / sp.duration).toFixed(2) + "%";
  }
  setInterval(renderProgress, 500);

  async function pollSpotify() {
    if (spBusy) return;
    try {
      const d = await api("/api/spotify/player");
      sp = d; spAt = Date.now();
      renderSpotify();
    } catch (e) {
      if (!sp) { sp = { connected: true, active: false }; renderSpotify(); }
    }
  }
  pollSpotify();
  setInterval(() => { if (document.visibilityState === "visible") pollSpotify(); }, 3000);

  async function cmd(action, body) {
    spBusy = true;
    try { await api("/api/spotify/" + action, body || {}); }
    catch (e) { toast(e.message); }
    finally { spBusy = false; setTimeout(pollSpotify, 350); }
  }
  $("play").addEventListener("click", () => {
    if (!sp) return;
    const was = sp.playing;
    if (sp.active) { sp.progress += was ? Date.now() - spAt : 0; spAt = Date.now(); sp.playing = !was; renderSpotify(); }
    cmd(was ? "pause" : "play");
  });
  $("next").addEventListener("click", () => cmd("next"));
  $("prev").addEventListener("click", () => cmd("previous"));
  $("shuffle").addEventListener("click", () => sp && cmd("shuffle", { state: !sp.shuffle }));
  $("repeat").addEventListener("click", () => {
    if (!sp) return;
    const nextState = { off: "context", context: "track", track: "off" }[sp.repeat || "off"];
    sp.repeat = nextState; renderSpotify();
    cmd("repeat", { state: nextState });
  });
  $("vol").addEventListener("input", () => {
    volDragging = true;
    const v = +$("vol").value; setVol(v);
    clearTimeout(volTimer);
    volTimer = setTimeout(() => { cmd("volume", { volume: v }).then(() => { volDragging = false; }); }, 250);
  });
  $("progress").addEventListener("click", (e) => {
    if (!sp || !sp.duration) return;
    const r = $("progress").getBoundingClientRect();
    const pos = Math.round(sp.duration * (e.clientX - r.left) / r.width);
    sp.progress = pos; spAt = Date.now(); renderProgress();
    cmd("seek", { position: pos });
  });

  // ---------- Claude plan quotas ----------
  let quota = null;
  function resetText(ts, long) {
    if (!ts) return "";
    const s = ts - (Date.now() + offsetMs) / 1000;
    if (s <= 0) return "resetting";
    const d = new Date(ts * 1000);
    const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (s < 3600) return (long ? "resets in " : "") + Math.max(1, Math.round(s / 60)) + "m";
    if (s < 86400) {
      const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
      return (long ? "resets in " : "") + h + "h" + (m ? " " + m + "m" : "") + (long ? " (" + hm + ")" : "");
    }
    const day = d.toLocaleDateString("en-GB", { weekday: "short" });
    return (long ? "resets " : "") + day + " " + hm;
  }
  function pickRows(limits) {
    const out = [], by = (k) => limits.find((l) => l.kind === k);
    if (by("session")) out.push(by("session"));
    if (by("weekly_all")) out.push(by("weekly_all"));
    const scoped = limits.filter((l) => l.kind !== "session" && l.kind !== "weekly_all")
      .sort((a, b) => b.percent - a.percent)[0];
    if (scoped) out.push(scoped);
    return out.length ? out : limits.slice(0, 3);
  }
  // The CSP (style-src 'self') blocks style="" attributes in HTML: set bar widths through the CSSOM instead.
  function applyWidths(root) {
    root.querySelectorAll("[data-w]").forEach((el) => { el.style.width = el.dataset.w + "%"; });
  }
  function renderQuota() {
    const el = $("quota");
    if (!quota) { el.hidden = true; return; }
    el.hidden = false;
    el.className = "quota";
    if (!quota.configured) { el.classList.add("note"); el.innerHTML = '<span class="q-note">Claude quotas · tap to sign in</span>'; return; }
    if (quota.error && !(quota.limits || []).length) { el.classList.add("note", "warn"); el.innerHTML = `<span class="q-note">${esc(quota.error)}</span>`; return; }
    el.innerHTML = pickRows(quota.limits || []).map((l) => `
      <span class="q-row ${esc(l.severity)}">
        <span class="q-top"><span class="q-name">${esc(l.short)}</span><b>${Math.round(l.percent)}%</b></span>
        <span class="q-bar"><i data-w="${Math.min(100, Math.max(0, l.percent))}"></i></span>
        <span class="q-reset">${esc(resetText(l.resets_at))}</span>
      </span>`).join("");
    applyWidths(el);
  }
  async function loadQuota(refresh) {
    try { quota = await api("/api/claude/quota" + (refresh ? "?refresh=1" : "")); }
    catch (e) { if (!quota) quota = { configured: true, error: e.message, limits: [] }; }
    renderQuota();
    return quota;
  }
  loadQuota();
  setInterval(() => { if (document.visibilityState === "visible") loadQuota(); }, 60000);
  setInterval(() => { if (quota && quota.limits) renderQuota(); }, 30000);   // keep "resets in" current

  function signInHtml(intro) {
    return `<p class="hint">${intro}</p>
      <ol class="q-steps">
        <li><button class="btn allow q-signin" id="qSignIn">Sign in to Claude</button>
          <span class="hint">Opens Claude in a new tab. Use the same account as on the computer.</span></li>
        <li><span class="hint">Claude then shows a code. Copy it, come back to this tab and paste it here:</span>
          <form class="cid" id="qCodeForm"><input id="qCode" placeholder="Paste the code" autocomplete="off" spellcheck="false" autocapitalize="off"><button class="chip">Connect</button></form>
          <p class="hint warn-text" id="qErr" hidden></p></li>
      </ol>
      <p class="hint">This sign-in can only read your plan's usage. It can't use Claude.</p>`;
  }
  function bindSignIn(b) {
    const err = (m) => { const e = b.querySelector("#qErr"); e.textContent = m; e.hidden = !m; };
    b.querySelector("#qSignIn").addEventListener("click", async () => {
      err("");
      const w = window.open("", "_blank");          // open now, while the tap still counts, then point it at Claude
      try {
        const { url } = await api("/api/claude/login", {});
        if (w) w.location.href = url; else location.href = url;
      } catch (e) { if (w) w.close(); err(e.message); }
    });
    b.querySelector("#qCodeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const code = b.querySelector("#qCode").value.trim();
      if (!code) { err("Paste the code first."); return; }
      const btn = e.target.querySelector("button"); btn.textContent = "…"; btn.disabled = true;
      try {
        quota = await api("/api/claude/finish", { code });
        renderQuota(); quotaSheet(); toast("Claude connected");
      } catch (e2) { err(e2.message); btn.textContent = "Connect"; btn.disabled = false; }
    });
  }
  function quotaSheet() {
    const q = quota || {};
    if (!q.configured) {
      openSheet(`<h4>Claude quotas</h4>${signInHtml("To show your plan's usage, the Remote server needs its own Claude sign-in. Two steps:")}`, bindSignIn);
      return;
    }
    if (q.login_needed) {
      openSheet(`<h4>Claude quotas</h4>${signInHtml(esc(q.error || "The sign-in has ended.") + " Sign in again:")}`, bindSignIn);
      return;
    }
    const rows = (q.limits || []).map((l) => `
      <div class="q-line ${esc(l.severity)}">
        <div class="q-top"><span>${esc(l.label)}</span><b>${Math.round(l.percent)}% used</b></div>
        <div class="q-bar big"><i data-w="${Math.min(100, Math.max(0, l.percent))}"></i></div>
        <div class="sub">${esc(resetText(l.resets_at, true))}</div>
      </div>`).join("");
    const ex = q.extra ? `<div class="q-line"><div class="q-top"><span>Extra usage this month</span><b>${q.extra.used.toFixed(2)} ${esc(q.extra.currency)}${q.extra.limit ? " of " + q.extra.limit.toFixed(2) : ""}</b></div></div>` : "";
    const when = q.updated ? "Updated " + ago(q.updated) + (ago(q.updated) === "now" ? "" : " ago") : "";
    openSheet(`<h4>Claude plan${q.plan ? " · " + esc(q.plan[0].toUpperCase() + q.plan.slice(1)) : ""}</h4>
      ${q.error ? `<p class="hint warn-text">${esc(q.error)}</p>` : ""}
      <div class="q-list">${rows || '<p class="hint">No limits reported.</p>'}${ex}</div>
      <div class="q-foot"><span class="hint">${esc(when)}</span><span><button class="chip ghost" id="qOut">Sign out</button> <button class="chip" id="qRefresh">Refresh</button></span></div>`, (b) => {
      b.querySelector("#qRefresh").addEventListener("click", async (e) => {
        e.target.textContent = "…"; await loadQuota(true); quotaSheet();
      });
      b.querySelector("#qOut").addEventListener("click", async () => {
        if (!confirm("Sign the Remote server out of Claude? The quotas stop showing until you sign in again.")) return;
        quota = await api("/api/claude/logout", {}); renderQuota(); quotaSheet();
      });
    });
  }
  $("quota").addEventListener("click", quotaSheet);

  // ---------- lyrics (LRCLIB, through the server) ----------
  const lyr = { open: false, key: "", data: null, cur: -1, els: [], userUntil: 0 };
  const trackKey = () => (sp && sp.active && sp.title ? sp.title + "\n" + sp.artist : "");

  function openLyrics() {
    if (!trackKey()) { toast("Nothing is playing"); return; }
    lyr.open = true;
    $("lyrics").hidden = false;
    try { history.pushState({ lyrics: 1 }, ""); } catch (e) {}
    loadLyrics();
  }
  function closeLyrics(fromHistory) {
    if (!lyr.open) return;
    lyr.open = false;
    $("lyrics").hidden = true;
    if (!fromHistory && history.state && history.state.lyrics) history.back();
  }
  window.addEventListener("popstate", () => closeLyrics(true));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLyrics(); });
  $("lyrBtn").addEventListener("click", openLyrics);
  $("art").addEventListener("click", openLyrics);
  $("lyrClose").addEventListener("click", () => closeLyrics());
  $("lyrPlay").addEventListener("click", () => $("play").click());

  function lyricsHead() {
    $("lyrTitle").textContent = sp.title || "";
    $("lyrArtist").textContent = sp.artist || "";
    if (sp.image) {
      if ($("lyrArt").getAttribute("src") !== sp.image) {
        $("lyrArt").src = sp.image;
        $("lyrBg").style.backgroundImage = 'url("' + sp.image.replace(/"/g, "") + '")';
      }
      $("lyrArt").hidden = false;
    } else { $("lyrArt").hidden = true; $("lyrBg").style.backgroundImage = "none"; }
    $("lyrPlayIcon").innerHTML = sp.playing ? PAUSE : PLAY;
  }
  function lyricsMessage(title, sub) {
    lyr.els = []; lyr.cur = -1;
    $("lyrics").classList.remove("plain");
    $("lyrLines").innerHTML = `<div class="lyr-msg"><div><b>${esc(title)}</b>${esc(sub || "")}</div></div>`;
  }
  async function loadLyrics() {
    if (!lyr.open || !sp) return;
    lyricsHead();
    const key = trackKey();
    if (!key) { lyricsMessage("Nothing is playing"); lyr.key = ""; return; }
    if (key === lyr.key) return;
    lyr.key = key; lyr.data = null;
    lyricsMessage("Looking for lyrics…");
    const q = new URLSearchParams({ title: sp.title, artist: sp.artist, album: sp.album || "", duration: String(sp.duration || 0) });
    let d;
    try { d = await api("/api/lyrics?" + q.toString()); }
    catch (e) { d = { found: false, error: e.message }; }
    if (lyr.key !== key) return;
    if (d.error) lyr.key = "";        // try again on the next update
    lyr.data = d;
    renderLyrics();
  }
  function renderLyrics() {
    const d = lyr.data;
    if (!d || !d.found) { lyricsMessage(d && d.error ? "Lyrics unavailable" : "No lyrics for this song", d && d.error ? d.error : "LRCLIB doesn't have this one."); return; }
    if (d.instrumental) { lyricsMessage("Instrumental", "No words in this one. Enjoy."); return; }
    $("lyrics").classList.toggle("plain", !d.synced);
    $("lyrLines").innerHTML = d.lines.map((l, i) =>
      `<p class="lyr-line${d.synced && !l.text ? " gap" : ""}" data-i="${i}">${esc(l.text)}</p>`).join("");
    lyr.els = Array.from($("lyrLines").children);
    lyr.cur = -1;
    $("lyrScroll").scrollTop = 0;
    syncLyrics(true);
  }
  function syncLyrics(jump) {
    const d = lyr.data;
    if (!lyr.open || !d || !d.synced || !lyr.els.length || !sp) return;
    const pos = sp.progress + (sp.playing ? Date.now() - spAt : 0) + 250;   // a little early reads better
    let i = -1;
    for (let k = 0; k < d.lines.length && d.lines[k].t <= pos; k++) i = k;
    if (i === lyr.cur && !jump) return;
    lyr.els.forEach((el, k) => { el.classList.toggle("now", k === i); el.classList.toggle("past", k < i); });
    lyr.cur = i;
    if (Date.now() < lyr.userUntil) return;
    const el = lyr.els[Math.max(0, i)], box = $("lyrScroll");
    const top = el.offsetTop - box.clientHeight * 0.38;
    if (jump) { box.style.scrollBehavior = "auto"; box.scrollTop = top; box.style.scrollBehavior = ""; }
    else box.scrollTo({ top, behavior: "smooth" });
  }
  setInterval(() => syncLyrics(false), 250);
  ["touchstart", "wheel"].forEach((ev) => $("lyrScroll").addEventListener(ev, () => { lyr.userUntil = Date.now() + 4000; }, { passive: true }));
  $("lyrLines").addEventListener("click", (e) => {
    const el = e.target.closest(".lyr-line");
    const d = lyr.data;
    if (!el || !d || !d.synced) return;
    const t = d.lines[+el.dataset.i].t;
    sp.progress = t; spAt = Date.now(); lyr.userUntil = 0;
    syncLyrics(true);
    cmd("seek", { position: t });
  });

  // ---------- sheets ----------
  function openSheet(html, bind) {
    $("sheetBody").innerHTML = html;
    applyWidths($("sheetBody"));
    $("sheetWrap").hidden = false;
    if (bind) bind($("sheetBody"));
  }
  function closeSheet() { $("sheetWrap").hidden = true; }
  $("sheetWrap").addEventListener("click", (e) => { if (e.target === $("sheetWrap")) closeSheet(); });

  const DEV_ICON = { Computer: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>', Smartphone: '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M12 18h.01"/>', Speaker: '<rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><path d="M12 6h.01"/>', TV: '<rect x="2" y="5" width="20" height="13" rx="2"/><path d="M8 21h8"/>' };

  $("devBtn").addEventListener("click", async () => {
    openSheet('<h4>Devices</h4><p class="hint">Loading…</p>');
    let devices = [], playlists = [];
    try {
      [devices, playlists] = await Promise.all([
        api("/api/spotify/devices").then((d) => d.devices),
        api("/api/spotify/playlists").then((d) => d.playlists).catch(() => []),
      ]);
    } catch (e) { openSheet('<h4>Devices</h4><p class="hint">' + esc(e.message) + "</p>"); return; }
    const devHtml = devices.length ? devices.map((d) => `
      <button class="row ${d.active ? "active" : ""}" data-dev="${esc(d.id)}">
        <span class="ph"><svg viewBox="0 0 24 24">${DEV_ICON[d.type] || DEV_ICON.Speaker}</svg></span>
        <span class="grow"><div class="nm">${esc(d.name)}</div><div class="sub">${esc(d.type)}${d.active ? " · playing here" : ""}</div></span>
      </button>`).join("") : '<p class="hint">No devices found. Open Spotify on a phone, computer or speaker first.</p>';
    const plHtml = playlists.map((p) => `
      <button class="row" data-pl="${esc(p.uri)}">
        ${p.image ? `<img src="${esc(p.image)}" alt="" loading="lazy">` : '<span class="ph"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span>'}
        <span class="grow"><div class="nm">${esc(p.name)}</div><div class="sub">${esc(p.owner)}</div></span>
      </button>`).join("");
    openSheet(`<div class="sheet-grid two"><div><h4>Play on</h4>${devHtml}</div><div><h4>Playlists</h4>${plHtml || '<p class="hint">No playlists</p>'}</div></div>`, (b) => {
      b.querySelectorAll("[data-dev]").forEach((el) => el.addEventListener("click", () => { closeSheet(); cmd("transfer", { device_id: el.dataset.dev }); }));
      b.querySelectorAll("[data-pl]").forEach((el) => el.addEventListener("click", () => {
        closeSheet();
        const dev = sp && sp.device && sp.device.id;
        cmd("play", dev ? { context_uri: el.dataset.pl, device_id: dev } : { context_uri: el.dataset.pl });
      }));
    });
  });

  function sw(key, label, sub) {
    return `<button class="row" data-pref="${key}"><span class="grow"><div class="nm">${label}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</span><span class="switch ${prefs[key] ? "on" : ""}"></span></button>`;
  }
  $("settingsBtn").addEventListener("click", () => {
    const wait = (snap && snap.settings.wait) || 600;
    const waits = [[60, "1 min"], [300, "5 min"], [600, "10 min"], [1800, "30 min"]];
    openSheet(`
      <div class="sheet-grid two"><div>
        <h4>Sounds on this phone</h4>
        ${sw("sound", "Sound alerts", "When Claude asks for approval or needs you")}
        ${sw("doneChime", "Chime when Claude finishes", "")}
        ${sw("nag", "Remind every 45 s", "While something is still waiting")}
        <div class="row"><svg viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z" class="fill"/></svg><input type="range" id="prefVol" min="0" max="100" value="${Math.round(prefs.volume * 100)}"><button class="chip" id="testSound">Test</button></div>
        <h4>Voice</h4>
        <button class="row" id="voiceSettingsBtn"><span class="grow"><div class="nm">Wake word and voice</div><div class="sub" id="voiceSub">${esc(window.RemoteVoice ? window.RemoteVoice.summary() : "")}</div></span><svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg></button>
      </div><div>
        <h4>Phone answers: wait for</h4>
        <div class="seg" id="waitSeg">${waits.map(([v, l]) => `<button data-wait="${v}" class="${v === wait ? "on" : ""}">${l}</button>`).join("")}</div>
        <p class="hint">After that, the prompt goes back to the computer.</p>
        <h4>Screen</h4>
        ${sw("wake", "Keep the screen on", "wakeLock" in navigator ? "" : "Not available over plain http: see the README")}
        <button class="row" id="fsBtn"><span class="grow"><div class="nm">Full screen</div></span><svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>
      </div></div>`, (b) => {
      b.querySelectorAll("[data-pref]").forEach((el) => el.addEventListener("click", () => {
        const k = el.dataset.pref; prefs[k] = !prefs[k]; savePrefs();
        el.querySelector(".switch").classList.toggle("on", prefs[k]);
        if (k === "wake") { if (prefs[k]) keepAwake(); else if (wakeLock) { wakeLock.release(); wakeLock = null; } }
      }));
      const pv = b.querySelector("#prefVol");
      pv.style.setProperty("--p", pv.value + "%");
      pv.addEventListener("input", () => { prefs.volume = pv.value / 100; pv.style.setProperty("--p", pv.value + "%"); savePrefs(); });
      b.querySelector("#testSound").addEventListener("click", () => { audio(); SOUNDS.permission(); });
      b.querySelectorAll("[data-wait]").forEach((el) => el.addEventListener("click", async () => {
        try { await api("/api/settings", { wait: +el.dataset.wait }); b.querySelectorAll("[data-wait]").forEach((x) => x.classList.toggle("on", x === el)); }
        catch (e) { toast(e.message); }
      }));
      b.querySelector("#voiceSettingsBtn").addEventListener("click", () => { if (window.RemoteVoice) window.RemoteVoice.openSettings(); });
      b.querySelector("#fsBtn").addEventListener("click", () => {
        if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => toast("Full screen is not available"));
        closeSheet();
      });
    });
  });

  // ---------- for voice.js ----------
  window.Remote = {
    api, toast, prefs, savePrefs, openSheet, closeSheet, audio, tone, sw,
    music: {
      playing: () => !!(sp && sp.active && sp.playing),
      pause: () => cmd("pause"),
      play: () => cmd("play"),
    },
  };

})();
