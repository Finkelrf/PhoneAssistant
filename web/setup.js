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
})();
