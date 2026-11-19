"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const toast = (m) => { const t = $("toast"); t.textContent = m; t.hidden = false; setTimeout(() => (t.hidden = true), 3000); };
  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Remote": "1" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "Request failed");
    return d;
  }
  function render(s) {
    $("pairUrl").textContent = s.phone_url;
    $("redirect").textContent = s.redirect_uri;
    $("cid").value = s.client_id || "";
    $("spState").innerHTML = s.connected ? '<b class="ok">Connected.</b> The phone can control Spotify.'
      : s.configured ? "Client ID saved. Now connect your Spotify account." : "Not set up yet.";
    $("connect").hidden = !s.configured || s.connected;
    $("disconnect").hidden = !s.connected;
    $("spSteps").hidden = s.connected;
    $("cidForm").hidden = s.connected;
    // away from the computer (the phone): the sign-in opens in a new tab and ends on an address to paste back here
    const away = location.hostname !== "127.0.0.1";
    $("phoneFinish").hidden = !away || !s.configured || s.connected;
    if (away) { $("connect").target = "_blank"; $("connect").rel = "noopener"; }
  }
  const load = () => api("/api/spotify/status").then(render).catch((e) => toast(e.message));
  $("cidForm").addEventListener("submit", (e) => { e.preventDefault(); api("/api/spotify/config", { client_id: $("cid").value }).then((s) => { render(s); toast("Saved"); }).catch((e) => toast(e.message)); });
  $("finishForm").addEventListener("submit", (e) => { e.preventDefault(); api("/api/spotify/finish", { url: $("cbUrl").value }).then((s) => { $("cbUrl").value = ""; render(s); toast("Spotify connected"); }).catch((e) => toast(e.message)); });
  $("disconnect").addEventListener("click", () => api("/api/spotify/disconnect", {}).then(render));
  $("copy").addEventListener("click", () => navigator.clipboard.writeText($("pairUrl").textContent).then(() => toast("Copied")));
  const q = new URLSearchParams(location.search).get("spotify");
  if (q === "ok") toast("Spotify connected"); else if (q === "cancelled") toast("Spotify sign-in cancelled");
  load();

  // ---------- voice ----------
  const clean = (t) => String(t || "").replace(/[<>&"]/g, "");
  function renderVoice(v) {
    $("pvKey").value = v.access_key || "";
    $("pvState").innerHTML = v.access_key ? '<b class="ok">AccessKey saved.</b> Porcupine word: “' + clean(v.keyword === "custom" ? v.custom_label : v.keyword) + "”."
      : "No AccessKey saved.";
    $("ppnRemove").hidden = !v.custom;
    if (v.custom && !$("ppnLabel").value) $("ppnLabel").value = v.custom_label;
    $("owwState").innerHTML = (v.engine === "openwakeword" ? '<b class="ok">In use.</b> ' : "") + "openWakeWord word: “" + clean(v.oww_label) + "”."
      + (v.oww_ready === false ? " The server's openWakeWord models are missing: rebuild the container." : "");
    $("owwRemove").hidden = !v.oww_custom;
    if (v.oww_custom && !$("owwLabel").value) $("owwLabel").value = v.oww_custom_label;
  }
  async function uploadFile(url, file, label) {
    const r = await fetch(url + "?label=" + encodeURIComponent(label || ""), { method: "POST", headers: { "X-Remote": "1", "Content-Type": "application/octet-stream" }, body: file });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "Upload failed");
    return d;
  }
  const loadVoice = () => api("/api/voice/config").then(renderVoice).catch((e) => toast(e.message));
  api("/api/links").then((l) => {
    const url = (l.secure_url || "") + "/";
    $("caLink").textContent = (l.public_url || "") + "/ca.crt";
    $("secureLink").textContent = l.secure_url ? url : "HTTPS is off on this server";
    $("secureLink").href = l.secure_url ? url : "#";
    $("secureState").innerHTML = window.isSecureContext && location.protocol === "https:" ? '<b class="ok">This page is on HTTPS: the certificate works here.</b>' : "";
  }).catch(() => {});
  $("keyForm").addEventListener("submit", (e) => { e.preventDefault(); api("/api/voice/config", { access_key: $("pvKey").value }).then((v) => { renderVoice(v); toast("Saved"); }).catch((e) => toast(e.message)); });
  $("ppnForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = $("ppnFile").files[0];
    if (!f) { toast("Pick the .ppn file first"); return; }
    try { renderVoice(await uploadFile("/api/voice/keyword", f, $("ppnLabel").value)); $("ppnFile").value = ""; toast("Wake word saved. Reload the phone page"); }
    catch (err) { toast(err.message); }
  });
  $("owwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = $("owwFile").files[0];
    if (!f) { toast("Pick the .onnx file first"); return; }
    try { renderVoice(await uploadFile("/api/voice/wakeword", f, $("owwLabel").value)); $("owwFile").value = ""; toast("Wake word saved. Reload the phone page"); }
    catch (err) { toast(err.message); }
  });
  $("owwRemove").addEventListener("click", () => api("/api/voice/wakeword/delete", {}).then((v) => { renderVoice(v); toast("Removed"); }));
  $("ppnRemove").addEventListener("click", () => api("/api/voice/keyword/delete", {}).then((v) => { renderVoice(v); toast("Removed"); }));
  loadVoice();
})();
