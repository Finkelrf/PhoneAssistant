# Remote

A page for an old phone on a stand (made for a Moto G5s, landscape 640×360, also works in portrait) that shows:

- **a clock**,
- **Claude Code sessions** on this computer: what each one is doing, and **Allow / Deny** buttons when Claude asks for permission, with **sound alerts** when Claude needs you,
- **Spotify controls**: now playing, play/pause, skip, seek, shuffle, repeat, volume, the device it plays on, and your playlists.
- **Lyrics** of the song that's playing, following the music line by line.

It runs only on the home network. There is no Cloudflare route, because answering permission prompts is close to running commands on this computer.

## How it works

```
Claude Code (desktop app / CLI)                  Docker: remote (port 8796)                    Phone
  hooks in ~/.claude/settings.json  ──POST──▶  server/ (Starlette)  ──Server-Sent Events──▶  web/ (page)
  hooks/claude_hook.py                          sessions, pending requests  ◀── Allow/Deny ──
                                                Spotify Web API proxy       ◀── controls ────
```

- `hooks/claude_hook.py` runs on the computer for each hook event (Python standard library only). `event` mode sends SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop and SessionEnd. `permission` mode (PermissionRequest) waits for the phone's answer.
- If the server is down or doesn't answer, the hook exits with no output, so Claude Code carries on as if there were no hook and the normal prompt shows on the computer.
- **Phone answers / At desk** (the switch at the top of the Claude panel; it starts on *At desk*):
  - *Phone answers*: a permission prompt goes to the phone and waits there (1, 5, 10 or 30 minutes, set in Settings). After that it goes back to the computer.
  - *At desk*: prompts stay on the computer, and the phone only rings and shows that the session needs you.
  - Switching to *At desk* hands any waiting prompts back to the computer straight away.
- **Sounds** (made in the page, no files):
  - a three-note alert for a permission request,
  - two notes when Claude needs you,
  - a soft chime when a turn finishes (can be turned off).
  - A reminder repeats every 45 s while something is still waiting. Settings has volume, test and on/off switches; these are kept on the phone.
- **Auto mode**: Claude Code ignores a hook's *Allow* while a session is in auto mode (or plan mode), so in those sessions only *Deny* from the phone takes effect.
- Spotify is called by the server, never by the phone. The sign-in uses Authorization Code with PKCE (no client secret). The refresh token is in `configs/remote/spotify.json`. Playback control needs Spotify Premium.
- **Lyrics** come from LRCLIB (lrclib.net), a free open lyrics database with no key; Spotify's Web API has no lyrics.
  - The Lyrics button in the player opens them full screen; in landscape it's the round button next to the volume. Tapping the album art also opens them, which helps when the screen is too short for the volume row.
  - The server asks LRCLIB with only the song's title, first artist, album and length (`GET /api/lyrics`), and keeps answers in memory.
  - Synced lyrics highlight the current line and scroll with the song, and tapping a line jumps the song there. Lyrics without timings are shown as plain text.
  - Android's back button closes the view.

## Files

- `server/app.py` - routes, the host check, the hook endpoints and the Spotify routes
- `server/state.py` - sessions and pending permission requests (`state.json` in the data folder)
- `server/spotify.py` - Spotify Web API client
- `server/lyrics.py` - lyrics from LRCLIB
- `web/` - the phone page (`index.html`, `app.css`, `app.js`) and the setup page (`setup.html`, `setup.js`), which works on the phone and the computer
- `hooks/claude_hook.py` - the Claude Code hook
- `tests/` - API tests, Spotify tests (mocked), and browser tests at 640×360 and 360×640 (screenshots in `tests/shots/`)

## Running

It is the `remote` service of the home-dashboard compose stack:

```bash
cd /home/rafael/Documents/home-dashboard
docker-compose up -d --build remote      # first start, or after any change here
docker logs -f remote
```

- Data (hook key `token`, `state.json`, `spotify.json`) lives in `home-dashboard/configs/remote`, not in this repository.
- `REMOTE_URL` (`http://192.168.1.163:8796`) is the phone's address: the setup page's QR code shows it, and only this host name (plus 127.0.0.1 and localhost) is answered. Change it if the computer's LAN address changes (a DHCP reservation on the router keeps it fixed).

### Access

There is no sign-in or pairing: any device on the home network can open `http://192.168.1.163:8796/` and use it, Allow and Deny included (the user's choice). The setup page has a QR code of that address for the phone. What stays protected:

- Hook calls (`/hook/*`) need the key in `configs/remote/token`, which `hooks/claude_hook.py` reads from the file. Without it, nothing else on the network can post fake sessions or prompts. Delete the file and restart to make a new key.
- The server answers only requests addressed to its own LAN address (from `REMOTE_URL`), `127.0.0.1` or `localhost`, so a web page can't reach it through DNS rebinding. Changes need an `X-Remote: 1` header, which other sites can't send to it.

### Spotify (once)

From the phone (tap **Set up Spotify** on its music panel, or open `http://192.168.1.163:8796/setup`) or from the
computer (`http://127.0.0.1:8796/setup`):

1. Create an app at developer.spotify.com/dashboard (Web API) with the Redirect URI `http://127.0.0.1:8796/spotify/callback`.
2. Paste its Client ID and press **Connect Spotify**, then sign in to Spotify and tap **Agree**.
3. On the computer, that's all: Spotify sends the browser back to the setup page.
   On the phone, Spotify opens in a new tab and ends on an `http://127.0.0.1:8796/spotify/callback?code=…` address that
   doesn't load (127.0.0.1 is the phone itself). Copy that whole address from the address bar, go back to the setup tab,
   paste it and press **Finish**; the server takes the one-time code from it (`POST /api/spotify/finish`).

Spotify only accepts `http` redirect URIs on 127.0.0.1, which is why the phone needs the copy-and-paste step. The code
is useless without the PKCE verifier the server keeps, and works once.

### Claude Code hooks

They are in `~/.claude/settings.json` (all sessions on this computer). `PermissionRequest` has a 3700 s timeout; the server decides how long it really waits. The others have a 5 s timeout.

```json
"hooks": {
  "PermissionRequest": [{"hooks": [{"type": "command", "command": "python3 /home/rafael/Documents/claude/remote/hooks/claude_hook.py permission", "timeout": 3700}]}],
  "SessionStart": [{"hooks": [{"type": "command", "command": "python3 /home/rafael/Documents/claude/remote/hooks/claude_hook.py event", "timeout": 5}]}],
  "...": "same for UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop, SessionEnd"
}
```

To turn it all off, remove those entries. Stopping the container is also safe: the hooks then do nothing.

### On the phone

- Open `http://192.168.1.163:8796/` in Chrome and tap once. That turns on sound (browsers only allow sound after a tap) and full screen.
- **Keep the screen on.** The Wake Lock API only works on https or localhost, so over `http://192.168.x.x` use one of these:
  - turn on Android *Developer options → Stay awake* (while charging), or
  - in Chrome, open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add `http://192.168.1.163:8796`, then relaunch. Wake Lock then works too.
- The page reconnects by itself, and a red "Reconnecting…" badge shows while it is offline.

## Tests

All tests run in Docker:

```bash
docker-compose run --rm --build test
```

They start the real server and run the real hook script against it. The browser tests check both phone orientations for overflow and take screenshots into `tests/shots/`.
