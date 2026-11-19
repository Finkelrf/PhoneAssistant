"use strict";
// Voice: say the wake word (Porcupine, in the browser) or tap the mic, ask a question (the browser's speech
// recognition), and Claude's answer is read aloud (the phone's own voice). Needs HTTPS for the microphone.
(() => {
  const R = window.Remote;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fakes = window.__voiceFakes || null;          // browser tests only
  const P = R.prefs;
  if (P.wakeWord === undefined) P.wakeWord = true;
  if (P.voiceLang === undefined) P.voiceLang = "en-US";
  if (P.voicePause === undefined) P.voicePause = true;
  if (P.voiceFollow === undefined) P.voiceFollow = true;
  if (P.voiceOut === undefined) P.voiceOut = "computer";     // who speaks the answer: "computer" (Piper) or "phone"
  const LANGS = [["en-US", "English (US)"], ["en-GB", "English (UK)"], ["pt-BR", "Português (Brasil)"], ["nl-NL", "Nederlands"], ["es-ES", "Español"]];
  const HISTORY_MS = 5 * 60 * 1000;

  let cfg = null;                 // /api/voice/config
  let wake = null;                // Porcupine worker
  let wakeState = "off";          // off | loading | on | error
  let wakeError = "";
  let busy = false;               // a conversation is going on
  let stopRequested = false;
  let history = [];               // [{q, a, at}]
  let pausedMusic = false;
  let recognizer = null;

  const secure = window.isSecureContext;
  const Recognition = (fakes && fakes.Recognition) || window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = (fakes && fakes.speech) || window.speechSynthesis;

  // ---------- small helpers ----------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function loadScript(src) {
    return new Promise((ok, bad) => {
      if (document.querySelector(`script[src="${src}"]`)) return ok();
      const s = document.createElement("script");
      s.src = src; s.onload = () => ok(); s.onerror = () => bad(new Error("Couldn't load " + src));
      document.head.appendChild(s);
    });
  }
  function earcon(up) {
    if (!R.prefs.sound) return;
    if (up) { R.tone(880, 0, 0.12, "sine", 0.18); R.tone(1320, 0.09, 0.18, "sine", 0.18); }
    else { R.tone(1175, 0, 0.12, "sine", 0.14); R.tone(784, 0.09, 0.2, "sine", 0.14); }
  }

  // ---------- overlay ----------
  function show(state, q, a) {
    $("voice").hidden = false;
    $("voice").dataset.state = state;
    $("voiceState").textContent = { listening: "Listening…", thinking: "Thinking…", speaking: "Claude", error: "Hmm", done: "Claude" }[state] || "";
    if (q !== undefined) $("voiceQ").textContent = q ? "“" + q + "”" : "";
    if (a !== undefined) { $("voiceA").textContent = a || ""; $("voiceA").scrollTop = 0; }
  }
  function hide() { $("voice").hidden = true; }

  function renderButton() {
    const btn = $("voiceBtn");
    btn.classList.toggle("off", !secure && !fakes);
    $("wakeDot").hidden = wakeState !== "on";
    btn.title = !secure && !fakes ? "Voice needs the HTTPS address" : wakeState === "on" ? "Listening for “" + wakeLabel() + "”. Tap to ask now" : "Ask Claude";
  }
  const isOww = () => !cfg || cfg.engine !== "porcupine";
  function wakeLabel() {
    if (!cfg) return "Hey Jarvis";
    if (isOww()) return cfg.oww_label;
    return cfg.keyword === "custom" ? cfg.custom_label : cfg.keyword;
  }
  // the wake word can start: openWakeWord needs nothing more, Porcupine needs its AccessKey
  const wakeConfigured = () => !!cfg && (isOww() ? cfg.oww_ready !== false : !!cfg.access_key);

  // openWakeWord: stream the mic (16 kHz, 16-bit) to the server, which says when it hears the word
  async function owwWake(onDetect) {
    let ws = null, ctx = null, stream = null, src = null, node = null, sink = null, closed = false, listening = false;
    const open = () => new Promise((ok, bad) => {
      let first = true;
      ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/wake/ws");
      ws.binaryType = "arraybuffer";
      ws.onmessage = (e) => {
        let m = {};
        try { m = JSON.parse(e.data); } catch (err) {}
        if (m.error) { if (first) { first = false; bad(new Error(m.error)); } return; }
        if (m.ready && first) { first = false; ok(); }
        if (m.wake && listening) onDetect(m);
      };
      ws.onerror = () => { if (first) { first = false; bad(new Error("Couldn't reach the wake word listener on the server")); } };
      ws.onclose = () => {
        if (first) { first = false; bad(new Error("The server closed the wake word listener")); return; }
        if (!closed) setTimeout(() => { if (!closed) open().then(() => { if (listening) ws.send("reset"); }).catch(() => {}); }, 3000);
      };
    });
    await open();
    return {
      async listen() {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        const A = window.AudioContext || window.webkitAudioContext;
        ctx = new A();
        await ctx.audioWorklet.addModule("/wake-worklet.js");
        src = ctx.createMediaStreamSource(stream);
        node = new AudioWorkletNode(ctx, "pcm16k", { processorOptions: { rate: ctx.sampleRate } });
        sink = ctx.createGain(); sink.gain.value = 0;          // keeps the graph running, makes no sound
        node.port.onmessage = (e) => { if (listening && ws && ws.readyState === 1) ws.send(e.data); };
        src.connect(node); node.connect(sink); sink.connect(ctx.destination);
        if (ctx.state === "suspended") await ctx.resume();
        if (ws && ws.readyState === 1) ws.send("reset");
        listening = true;
      },
      async pause() {
        listening = false;
        if (stream) stream.getTracks().forEach((t) => t.stop());     // hand the mic to speech recognition
        if (ctx) await ctx.close().catch(() => {});
        stream = ctx = src = node = sink = null;
      },
      async stop() { closed = true; await this.pause(); if (ws) ws.close(); },
    };
  }

  // ---------- wake word ----------
  async function startWake() {
    if (wake || wakeState === "loading") return;
    if (!P.wakeWord || !wakeConfigured() || (!secure && !fakes)) { wakeState = "off"; renderButton(); return; }
    wakeState = "loading"; wakeError = ""; renderButton();
    try {
      const onDetect = () => { if (!busy) converse(true); };
      if (fakes && fakes.wake) {
        wake = await fakes.wake(onDetect);
      } else if (isOww()) {
        wake = await owwWake(onDetect);
      } else {
        await loadScript("/vendor/web-voice-processor-4.0.10.min.js");
        await loadScript("/vendor/porcupine-web-4.0.1.min.js");
        const kw = cfg.keyword === "custom"
          ? { publicPath: "/api/voice/keyword.ppn", label: cfg.custom_label, sensitivity: cfg.sensitivity,
              customWritePath: "remote_keyword", forceWrite: true, version: cfg.version }
          : { builtin: cfg.keyword, sensitivity: cfg.sensitivity };
        const worker = await window.PorcupineWeb.PorcupineWorker.create(cfg.access_key, [kw], onDetect,
          { publicPath: "/vendor/porcupine_params-4.0.pv", customWritePath: "remote_params_4_0", version: 1 },
          { processErrorCallback: (e) => { wakeError = String((e && e.message) || e); } });
        wake = {
          listen: () => window.WebVoiceProcessor.WebVoiceProcessor.subscribe(worker),
          pause: () => window.WebVoiceProcessor.WebVoiceProcessor.unsubscribe(worker),
          stop: async () => { await window.WebVoiceProcessor.WebVoiceProcessor.unsubscribe(worker); await worker.terminate(); },
        };
      }
      await wake.listen();
      wakeState = "on";
    } catch (e) {
      wake = null; wakeState = "error";
      wakeError = friendlyWakeError(e);
      R.toast("Wake word: " + wakeError);
    }
    renderButton();
  }
  async function stopWake() {
    const w = wake; wake = null; wakeState = "off";
    if (w) { try { await w.stop(); } catch (e) {} }
    renderButton();
  }
  function friendlyWakeError(e) {
    const m = String((e && e.message) || e || "");
    if (/wake word listener|openWakeWord|model/i.test(m)) return m;
    if (/AccessKey|ActivationRefused|ActivationError|activation/i.test(m)) return "Picovoice refused the AccessKey. Check it on the setup page.";
    if (/ActivationLimit|limit/i.test(m)) return "the Picovoice AccessKey reached its device limit.";
    if (/Permission|NotAllowed|denied/i.test(m)) return "the microphone is blocked for this page.";
    if (/NotFound|device/i.test(m)) return "no microphone found.";
    return m.slice(0, 120) || "couldn't start.";
  }

  // ---------- one question ----------
  function listen() {
    return new Promise((resolve) => {
      if (!Recognition) { resolve({ error: "This browser can't recognise speech" }); return; }
      const r = new Recognition();
      recognizer = r;
      r.lang = P.voiceLang; r.interimResults = true; r.continuous = false; r.maxAlternatives = 1;
      let final = "", interim = "", done = false;
      const finish = (res) => { if (!done) { done = true; recognizer = null; resolve(res); } };
      r.onresult = (ev) => {
        interim = "";
        for (let i = ev.resultIndex; i < ev.results.length; i++) {
          const t = ev.results[i][0].transcript;
          if (ev.results[i].isFinal) final += t; else interim += t;
        }
        $("voiceQ").textContent = (final + interim).trim();
      };
      r.onerror = (ev) => {
        const code = ev && ev.error;
        if (code === "no-speech" || code === "aborted") finish({ text: "" });
        else if (code === "not-allowed" || code === "service-not-allowed") finish({ error: "The microphone is blocked for this page" });
        else if (code === "network") finish({ error: "Speech recognition needs the internet" });
        else finish({ error: "Didn't catch that (" + (code || "error") + ")" });
      };
      r.onend = () => finish({ text: (final || interim).trim() });
      try { r.start(); } catch (e) { finish({ error: "Couldn't start listening" }); }
    });
  }

  let playing = null;              // the computer voice's audio source, while it plays

  // The answer, spoken by Piper on the computer and played through the page's audio (unlocked by the first tap)
  async function speakComputer(text) {
    const r = await fetch("/api/voice/speak", { method: "POST", headers: { "Content-Type": "application/json", "X-Remote": "1" },
      body: JSON.stringify({ text, lang: P.voiceLang }) });
    if (!r.ok) { let m = ""; try { m = (await r.json()).error; } catch (e) {} throw new Error(m || "The computer voice failed (" + r.status + ")"); }
    const data = await r.arrayBuffer();
    if (stopRequested) return;
    const ctx = R.audio();
    if (!ctx) throw new Error("No audio on this page");
    const buf = await new Promise((ok, bad) => { const pr = ctx.decodeAudioData(data, ok, bad); if (pr && pr.catch) pr.catch(bad); });
    if (stopRequested) return;
    await new Promise((resolve) => {
      const src = ctx.createBufferSource();
      const gain = ctx.createGain();
      gain.gain.value = Math.max(0.15, R.prefs.volume || 0.8);
      src.buffer = buf;
      src.connect(gain).connect(ctx.destination);
      let done = false;
      const end = () => { if (!done) { done = true; if (playing === src) playing = null; resolve(); } };
      src.onended = end;
      playing = src;
      src.start();
      setTimeout(end, buf.duration * 1000 + 1500);            // never hang on a lost "ended"
    });
  }

  // The phone's own voice (Android's text-to-speech)
  function speakPhone(text) {
    return new Promise((resolve) => {
      if (!synth || !window.SpeechSynthesisUtterance && !fakes) { resolve(); return; }
      // cancel() right before speak() makes Chrome drop the new utterance on some phones: only when needed
      try { if (synth.speaking || synth.pending) synth.cancel(); } catch (e) {}
      const U = (fakes && fakes.Utterance) || window.SpeechSynthesisUtterance;
      const u = new U(text);
      u.lang = P.voiceLang;
      const voices = (synth.getVoices && synth.getVoices()) || [];
      const v = voices.find((x) => x.lang === P.voiceLang) || voices.find((x) => x.lang && x.lang.replace("_", "-").slice(0, 2) === P.voiceLang.slice(0, 2));
      if (v) u.voice = v;
      let done = false;
      const end = () => { if (!done) { done = true; clearInterval(keep); resolve(); } };
      u.onend = end;
      u.onerror = (e) => { if (e && e.error && e.error !== "interrupted" && e.error !== "canceled") R.toast("Phone voice: " + e.error); end(); };
      // desktop Chrome stops long speech after ~15 s unless nudged; on Android pause() ends it, so not there
      const android = /Android/i.test(navigator.userAgent);
      const keep = android ? 0 : setInterval(() => { if (synth.speaking && synth.pause && synth.resume) { synth.pause(); synth.resume(); } }, 10000);
      setTimeout(() => synth.speak(u), 60);
      setTimeout(end, Math.min(120000, 4000 + text.length * 120));
    });
  }

  async function speak(text) {
    if (P.voiceOut !== "phone") {
      try { await speakComputer(text); return; }
      catch (e) { if (stopRequested) return; R.toast(e.message + ". Using the phone's voice"); }
    }
    await speakPhone(text);
  }

  async function converse(fromWake) {
    if (busy) return;
    if (!secure && !fakes) { needsHttps(); return; }
    busy = true; stopRequested = false;
    if (wake) { try { await wake.pause(); } catch (e) {} }
    if (P.voicePause && R.music.playing()) { pausedMusic = true; R.music.pause(); }
    if (Date.now() - (history.length ? history[history.length - 1].at : 0) > HISTORY_MS) history = [];
    try {
      let follow = true;
      while (follow && !stopRequested) {
        follow = false;
        show("listening", "", "");
        earcon(true);
        await sleep(fromWake ? 250 : 150);         // let the mic hand over and the beep finish
        const heard = await listen();
        if (stopRequested) break;
        if (heard.error) { show("error", "", heard.error); await sleep(2500); break; }
        if (!heard.text) { earcon(false); break; }
        show("thinking", heard.text, "");
        let answer;
        try {
          const r = await R.api("/api/voice/ask", { text: heard.text, history: history.map(({ q, a }) => ({ q, a })) });
          answer = r.answer;
        } catch (e) {
          if (stopRequested) break;
          show("error", heard.text, e.message);
          await speak(e.message);
          await sleep(1200);
          break;
        }
        if (stopRequested) break;
        history.push({ q: heard.text, a: answer, at: Date.now() });
        history = history.slice(-6);
        show("speaking", heard.text, answer);
        await speak(answer);
        if (stopRequested) break;
        show("done", heard.text, answer);
        // Claude asked something back: listen once more without the wake word
        follow = P.voiceFollow && /\?\s*$/.test(answer);
        if (!follow) await sleep(4000);
      }
    } finally {
      if (!stopRequested) hide();
      busy = false;
      if (pausedMusic) { pausedMusic = false; R.music.play(); }
      if (wake) { try { await wake.listen(); } catch (e) {} }
    }
  }

  function stop() {
    stopRequested = true;
    if (recognizer) { try { recognizer.abort(); } catch (e) {} }
    try { synth && synth.cancel(); } catch (e) {}
    if (playing) { try { playing.stop(); } catch (e) {} playing = null; }
    hide();
  }

  function needsHttps() {
    R.openSheet(`<h4>Voice needs the secure address</h4>
      <p class="hint">Phones only give a page the microphone over HTTPS. Install Remote's certificate once, then open the secure address. The setup page walks you through it.</p>
      <div class="row-btns"><a class="chip" href="/setup#voice">Open setup</a>${cfg && cfg.secure_url ? `<a class="chip" href="${esc(cfg.secure_url)}/">Open ${esc(cfg.secure_url)}</a>` : ""}</div>`);
  }

  // ---------- settings ----------
  function summary() {
    if (!secure && !fakes) return "Needs the HTTPS address";
    if (!cfg) return "";
    if (!wakeConfigured()) return isOww() ? "Wake word models are missing on the server. Tap the mic to ask" : "Add a Picovoice AccessKey on the setup page, or switch to openWakeWord";
    if (!P.wakeWord) return "Wake word off. Tap the mic to ask";
    return wakeState === "on" ? "Listening for “" + wakeLabel() + "”" : wakeState === "error" ? "Wake word: " + wakeError : "Wake word: “" + wakeLabel() + "”";
  }
  function mailHtml() {
    if (cfg && cfg.linked) {
      return `<div class="row"><span class="grow"><div class="nm">Linked on this phone</div><div class="sub">Ask about your Gmail and Google Calendar. Claude asks you to confirm before it sends or changes anything.</div></span></div>
        <div class="row-btns"><button class="chip" id="vUnlink">Unlink this phone</button></div>`;
    }
    if (!cfg || !cfg.secure) return '<p class="hint">Open the secure (HTTPS) address on this phone to link it.</p>';
    return `<p class="hint">On the computer, run <b>python3 remote/hooks/link_phone.py</b> and type the code here.</p>
      <div class="cid" id="vLinkRow"><input id="vCode" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="12" placeholder="ABCD-1234" aria-label="Link code"><button class="chip" id="vLink">Link</button></div>`;
  }
  function openSettings() {
    const oww = isOww();
    let opts = "";
    if (cfg && oww) {
      opts = cfg.oww_models.map((m) => `<option value="${esc(m.id)}" ${cfg.oww_model === m.id ? "selected" : ""}>${esc(m.label)}</option>`).join("")
        + (cfg.oww_custom ? `<option value="custom" ${cfg.oww_model === "custom" ? "selected" : ""}>${esc(cfg.oww_custom_label)}</option>` : "");
    } else if (cfg) {
      opts = cfg.built_in.map((k) => `<option value="${esc(k)}" ${cfg.keyword === k ? "selected" : ""}>${esc(k)}</option>`).join("")
        + (cfg.custom ? `<option value="custom" ${cfg.keyword === "custom" ? "selected" : ""}>${esc(cfg.custom_label)}</option>` : "");
    }
    const ready = wakeConfigured();
    R.openSheet(`
      <div class="sheet-grid two"><div>
        <h4>Wake word</h4>
        <div class="seg" id="vEngine"><button data-engine="openwakeword" class="${oww ? "on" : ""}">openWakeWord</button><button data-engine="porcupine" class="${oww ? "" : "on"}">Picovoice</button></div>
        <p class="hint">${oww ? "Heard by this computer: the phone streams its microphone over the home network." : "Heard on the phone itself. Needs a Picovoice AccessKey (setup page)."}</p>
        ${R.sw("wakeWord", "Listen for the wake word", ready ? "Keeps the microphone on while this page is open" : oww ? "Not available right now" : "Add a Picovoice AccessKey on the setup page first")}
        <label class="row"><span class="grow"><div class="nm">Word</div></span><select id="vKeyword" ${ready ? "" : "disabled"}>${opts}</select></label>
        <label class="row"><span class="grow"><div class="nm">Sensitivity</div><div class="sub">Higher hears it more easily, with more false starts</div></span><input type="range" id="vSens" min="10" max="80" value="${Math.round(((cfg && cfg.sensitivity) || 0.5) * 100)}"></label>
        <p class="hint" id="vStatus">${esc(summary())}</p>
        ${oww && cfg && cfg.oww_ready !== false ? '<div class="row-btns"><button class="chip" id="vTrain">Train a new wake word</button></div>' : ""}
      </div><div>
        <h4>Talking</h4>
        <label class="row"><span class="grow"><div class="nm">Voice</div></span><select id="vOut"><option value="computer" ${P.voiceOut !== "phone" ? "selected" : ""}>This computer</option><option value="phone" ${P.voiceOut === "phone" ? "selected" : ""}>The phone</option></select></label>
        <label class="row"><span class="grow"><div class="nm">Language</div></span><select id="vLang">${LANGS.map(([v, l]) => `<option value="${v}" ${P.voiceLang === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        ${R.sw("voicePause", "Pause the music while talking", "")}
        ${R.sw("voiceFollow", "Listen again after a question", "")}
        <div class="row-btns"><button class="chip" id="vTest">Test the voice</button><a class="chip" href="/setup#voice">Setup</a></div>
        <h4>Email and calendar</h4>
        ${mailHtml()}
      </div></div>`, (b) => {
      b.querySelectorAll("[data-pref]").forEach((el) => el.addEventListener("click", () => {
        const k = el.dataset.pref; P[k] = !P[k]; R.savePrefs();
        el.querySelector(".switch").classList.toggle("on", P[k]);
        if (k === "wakeWord") { (P.wakeWord ? startWake() : stopWake()).then(() => { b.querySelector("#vStatus").textContent = summary(); }); }
      }));
      const save = async (body, reopen) => {
        try { cfg = Object.assign(cfg || {}, await R.api("/api/voice/config", body)); await stopWake(); await startWake(); }
        catch (e) { R.toast(e.message); }
        if (reopen) openSettings(); else b.querySelector("#vStatus").textContent = summary();
      };
      b.querySelectorAll("[data-engine]").forEach((el) => el.addEventListener("click", () => {
        if ((el.dataset.engine === "openwakeword") !== oww) save({ engine: el.dataset.engine }, true);
      }));
      b.querySelector("#vKeyword").addEventListener("change", (e) => save(oww ? { oww_model: e.target.value } : { keyword: e.target.value }));
      const sens = b.querySelector("#vSens");
      sens.style.setProperty("--p", ((sens.value - 10) / 70 * 100) + "%");
      sens.addEventListener("input", () => sens.style.setProperty("--p", ((sens.value - 10) / 70 * 100) + "%"));
      sens.addEventListener("change", () => save({ sensitivity: sens.value / 100 }));
      b.querySelector("#vLang").addEventListener("change", (e) => { P.voiceLang = e.target.value; R.savePrefs(); });
      b.querySelector("#vOut").addEventListener("change", (e) => { P.voiceOut = e.target.value; R.savePrefs(); });
      const trainBtn = b.querySelector("#vTrain");
      if (trainBtn) trainBtn.addEventListener("click", () => openTrain());
      const linkBtn = b.querySelector("#vLink");
      if (linkBtn) linkBtn.addEventListener("click", async () => {
        try { await R.api("/api/phone/link", { code: b.querySelector("#vCode").value }); cfg.linked = true; R.toast("Email and calendar linked"); openSettings(); }
        catch (e) { R.toast(e.message); }
      });
      const unlinkBtn = b.querySelector("#vUnlink");
      if (unlinkBtn) unlinkBtn.addEventListener("click", async () => {
        try { await R.api("/api/phone/unlink", {}); cfg.linked = false; openSettings(); }
        catch (e) { R.toast(e.message); }
      });
      b.querySelector("#vTest").addEventListener("click", () => { stopRequested = false; speak(P.voiceLang.startsWith("pt") ? "Oi! Estou ouvindo." : P.voiceLang.startsWith("nl") ? "Hallo! Ik luister." : P.voiceLang.startsWith("es") ? "¡Hola! Te escucho." : "Hi! I'm listening."); });
    });
  }

  // ---------- training a new wake word (openWakeWord, on the computer: server/trainer.py) ----------
  let trainTimer = null;
  let recording = false;

  // one recording from the microphone, 16 kHz 16-bit, the same way the wake word listener hears it
  async function recordPcm(ms) {
    if (fakes && fakes.record) return fakes.record(ms);
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const A = window.AudioContext || window.webkitAudioContext;
    const ctx = new A();
    try {
      await ctx.audioWorklet.addModule("/wake-worklet.js");
      const src = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, "pcm16k", { processorOptions: { rate: ctx.sampleRate } });
      const sink = ctx.createGain(); sink.gain.value = 0;
      const blocks = [];
      node.port.onmessage = (e) => blocks.push(new Int16Array(e.data));
      src.connect(node); node.connect(sink); sink.connect(ctx.destination);
      if (ctx.state === "suspended") await ctx.resume();
      await sleep(ms);
      const out = new Int16Array(blocks.reduce((n, x) => n + x.length, 0));
      let o = 0;
      blocks.forEach((x) => { out.set(x, o); o += x.length; });
      return out;
    } finally {
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
    }
  }

  function trainResult(r) {
    if (!r) return "";
    const heard = r.recall != null ? Math.round(r.recall * 100) + "% of test clips" : "";
    const wakes = r.per_hour == null ? "" : r.per_hour < 0.05 ? "no false wakes in " + r.hours + " h of other audio" : r.per_hour + " false wakes per hour of other audio";
    return [heard && "Heard " + heard, wakes].filter(Boolean).join(", ");
  }

  function trainedListHtml() {
    const items = ((cfg && cfg.oww_models) || []).filter((m) => m.trained);
    if (!items.length) return '<p class="hint">None yet.</p>';
    return items.map((m) => `<div class="row"><span class="grow"><div class="nm">${esc(m.label)}${cfg.oww_model === m.id ? " · in use" : ""}</div><div class="sub">${esc(trainResult(m.result))}</div></span>
      ${cfg.oww_model === m.id ? "" : `<button class="chip" data-use="${esc(m.id)}">Use</button>`}<button class="chip ghost" data-del="${esc(m.id)}" aria-label="Delete ${esc(m.label)}">Delete</button></div>`).join("");
  }

  function trainStateHtml(st) {
    if (st.state === "running") {
      const pct = Math.round((st.progress || 0) * 100);
      const mins = st.started ? Math.max(0, Math.round((Date.now() / 1000 - st.started) / 60)) : 0;
      return `<div class="row"><span class="grow"><div class="nm">Training “${esc(st.phrase)}”</div><div class="sub">${esc(st.step || "")} · ${pct}% · ${mins} min</div></span></div>
        <div class="progress train-bar"><div class="bar" style="width:${pct}%"></div></div>
        <div class="row-btns"><button class="chip" id="tCancel">Cancel</button></div>
        <p class="hint">You can close this. Training goes on on the computer.</p>`;
    }
    let msg = "";
    if (st.state === "done") {
      const m = ((cfg && cfg.oww_models) || []).find((x) => x.id === "t_" + st.id);
      msg = `<div class="row"><span class="grow"><div class="nm">“${esc(st.phrase)}” is ready</div><div class="sub">${esc(trainResult(st.result))}</div></span>
        ${m && cfg.oww_model !== m.id ? `<button class="chip" data-use="${esc(m.id)}">Use it</button>` : ""}</div>`;
    } else if (st.state === "error") msg = `<p class="hint">${esc(st.error || "Training failed")}</p>`;
    else if (st.state === "cancelled") msg = '<p class="hint">Training was cancelled.</p>';
    return msg + '<div class="row-btns"><button class="chip" id="tStart">Start training</button></div>';
  }

  async function openTrain() {
    clearTimeout(trainTimer);
    let st = { state: "idle", samples: 0, max_samples: 20 };
    try { st = await R.api("/api/wake/train"); } catch (e) { R.toast(e.message); }
    R.openSheet(`
      <div class="sheet-grid two"><div>
        <h4>Train a wake word</h4>
        <p class="hint">The computer makes thousands of examples with synthetic voices and trains a small model. It takes about 15 minutes. English phrases of two or three words work best, like “Hey Claude”.</p>
        <div class="cid"><input id="tPhrase" maxlength="40" autocomplete="off" spellcheck="false" placeholder="Hey Claude" aria-label="Wake word" value="${esc(st.state === "running" ? st.phrase : "")}"></div>
        <h4>Your voice (optional)</h4>
        <p class="hint">Say the wake word 5 to 10 times from where the phone stands, one recording each. It helps the model hear you.</p>
        <div class="row-btns"><button class="chip" id="tRec">Record</button><button class="chip ghost" id="tClear">Clear</button></div>
        <p class="hint" id="tCount"></p>
      </div><div>
        <h4>Training</h4>
        <div id="tState"></div>
        <h4>Trained wake words</h4>
        <div id="tList"></div>
        <div class="row-btns"><button class="chip ghost" id="tBack">Back</button></div>
      </div></div>`, (b) => {
      const count = (n) => { b.querySelector("#tCount").textContent = n ? n + (n === 1 ? " recording" : " recordings") + " of you" : "No recordings yet"; };
      const use = async (id) => {
        try { cfg = Object.assign(cfg || {}, await R.api("/api/voice/config", { oww_model: id, engine: "openwakeword" })); await stopWake(); await startWake(); R.toast("Wake word: " + wakeLabel()); }
        catch (e) { R.toast(e.message); }
        render(st);
      };
      const render = (s) => {
        st = s;
        if (!document.body.contains(b.querySelector("#tState")) || $("sheetWrap").hidden) { clearTimeout(trainTimer); return; }
        b.querySelector("#tState").innerHTML = trainStateHtml(s);
        b.querySelector("#tList").innerHTML = trainedListHtml();
        b.querySelector("#tPhrase").disabled = s.state === "running";
        b.querySelector("#tRec").disabled = s.state === "running" || recording;
        count(s.samples || 0);
        b.querySelectorAll("[data-use]").forEach((el) => el.addEventListener("click", () => use(el.dataset.use)));
        b.querySelectorAll("[data-del]").forEach((el) => el.addEventListener("click", async () => {
          try { cfg = Object.assign(cfg || {}, await R.api("/api/voice/wakeword/trained/delete", { id: el.dataset.del.slice(2) })); await stopWake(); await startWake(); }
          catch (e) { R.toast(e.message); }
          render(st);
        }));
        const start = b.querySelector("#tStart");
        if (start) start.addEventListener("click", async () => {
          try { render(await R.api("/api/wake/train", { phrase: b.querySelector("#tPhrase").value })); poll(); }
          catch (e) { R.toast(e.message); }
        });
        const cancel = b.querySelector("#tCancel");
        if (cancel) cancel.addEventListener("click", async () => { try { render(await R.api("/api/wake/train/cancel", {})); } catch (e) { R.toast(e.message); } });
      };
      const poll = () => {
        clearTimeout(trainTimer);
        if (st.state !== "running") return;
        trainTimer = setTimeout(async () => {
          try {
            const s = await R.api("/api/wake/train");
            if (s.state === "done" && st.state === "running") {
              cfg = Object.assign(cfg || {}, await R.api("/api/voice/config"));
              if (R.prefs.sound) { R.tone(1047, 0, 0.9, "sine", 0.22); R.tone(1568, 0.06, 0.8, "sine", 0.08); }
            }
            render(s);
          } catch (e) {}
          poll();
        }, 2000);
      };
      b.querySelector("#tRec").addEventListener("click", async () => {
        if (recording) return;
        recording = true;
        const btn = b.querySelector("#tRec");
        btn.disabled = true;
        const hadWake = !!wake;
        try {
          if (wake) await wake.pause();
          const phrase = b.querySelector("#tPhrase").value.trim() || "the wake word";
          btn.textContent = "Get ready…";
          await sleep(500);
          R.tone(880, 0, 0.12, "sine", 0.18);
          btn.textContent = "Say “" + phrase + "”";
          const pcm = await recordPcm(2600);
          btn.textContent = "Saving…";
          const r = await fetch("/api/wake/train/sample", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Remote": "1" }, body: pcm.buffer });
          let d = {};
          try { d = await r.json(); } catch (e) {}
          if (!r.ok) throw new Error(d.error || "Couldn't save the recording (" + r.status + ")");
          st.samples = d.samples;
          count(d.samples);
        } catch (e) {
          R.toast(e.message || String(e));
        } finally {
          recording = false;
          btn.textContent = "Record";
          btn.disabled = st.state === "running";
          if (hadWake && wake) { try { await wake.listen(); } catch (e) {} }
        }
      });
      b.querySelector("#tClear").addEventListener("click", async () => {
        try { count((await R.api("/api/wake/train/samples/clear", {})).samples); st.samples = 0; } catch (e) { R.toast(e.message); }
      });
      b.querySelector("#tBack").addEventListener("click", () => { clearTimeout(trainTimer); openSettings(); });
      render(st);
      poll();
    });
  }

  // ---------- wiring ----------
  $("voiceBtn").addEventListener("click", () => converse(false));
  $("voiceClose").addEventListener("click", stop);
  $("voiceAgain").addEventListener("click", () => { stop(); setTimeout(() => converse(false), 200); });
  // the wake word needs a tap first (audio and microphone start only after one)
  $("start").addEventListener("click", () => { startWake(); });

  async function load() {
    try {
      const [c, links] = await Promise.all([R.api("/api/voice/config"), R.api("/api/links")]);
      cfg = Object.assign(c, { secure_url: links.secure_url });
    } catch (e) { cfg = null; }
    renderButton();
    if ($("start").hidden) startWake();
  }
  load();

  window.RemoteVoice = { summary, openSettings, openTrain, converse, stop, state: () => ({ wakeState, busy, history: history.length }) };
})();
