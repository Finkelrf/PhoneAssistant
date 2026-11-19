# Remote

A page for an old phone on a stand (made for a Moto G5s, landscape 640×360, also works in portrait) that shows:

- **a clock**,
- **Claude Code sessions** on this computer: what each one is doing, and **Allow / Deny** buttons when Claude asks for permission, with **sound alerts** when Claude needs you,
- **Spotify controls**: now playing, play/pause, skip, seek, shuffle, repeat, volume, the device it plays on, and your playlists.
- **Lyrics** of the song that's playing, following the music line by line.
- **Claude plan quotas**: how much of the current 5-hour session and of the week (all models, and per model) is used, and when each resets.
- **Voice**: say a wake word (or tap the mic) and ask Claude a question; the answer is read aloud.

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

## Voice

Say the wake word ("Hey Jarvis" by default, or your own such as "Hey Claude") or tap the mic in the Claude panel, then ask.

```
Phone (https://192.168.1.163:8797)                                       Docker: remote
  mic, 16 kHz (web/wake-worklet.js)            ──wss /api/wake/ws──▶      openWakeWord (server/wakeword.py)
                                               ◀── {"wake": ...} ───
  Chrome's speech recognition: question → text ──POST /api/voice/ask──▶  claude -p (web search only)
                                               ◀────── answer ──────     server's own Claude login
  plays the spoken answer                      ──POST /api/voice/speak─▶ Piper (server/tts.py) → WAV
```

- **Wake word**: two engines; pick one on the phone in Settings › Voice (saved on the server).
  - **openWakeWord** (default, no account). The phone streams its microphone (16 kHz, 16-bit, 80 ms blocks) over a WebSocket to the server, which runs openWakeWord's ONNX models with onnxruntime (`server/wakeword.py`, a small port of openWakeWord's streaming code, Apache-2.0). Audio stays on the home network; the server keeps only the last fraction of a second of it, in memory.
    - Ready-made words: Hey Jarvis, Alexa, Hey Mycroft, Hey Rhasspy. Their models (openWakeWord release v0.5.1, CC BY-NC-SA 4.0: fine for personal use) are downloaded and hash-checked when the image is built, not kept in this repository.
    - Your own word, trained on the phone: Settings › Wake word and voice › **Train a new wake word**. Type the phrase, optionally record yourself saying it 5 to 10 times, and start. The computer trains it in about 15 minutes, on its CPU (`server/trainer.py`, run as its own low-priority process, one at a time):
      - positives: the phrase said by Piper voices (`en_US-libritts_r-medium` has 904 speakers, plus the other English voices) at random speeds and styles, and your recordings;
      - negatives: about 10 hours of real speech, music and noise as openWakeWord features (`validation_set_features.npy` from openWakeWord's feature dataset, same licence, downloaded at build time), plus Piper saying parts of the phrase, words that sound close to it, and everyday sentences;
      - each clip gets phone-like filtering, room echo, background noise (coloured noise, and the room heard before you spoke in your recordings) and a random level;
      - a small network (16 embeddings → 64 → 64 → score) is trained with numpy and saved as ONNX in `configs/remote/wakewords/`. The sheet shows how often it heard held-out examples and how many false wakes per hour it had on held-out real audio. Up to 8 trained words are kept; pick one in the Word list or on the training sheet.
      - Your recordings (`configs/remote/wake_train/`, mode 600) are deleted when training ends. English phrases only (the voices are English).
    - Or upload an `.onnx` made with openWakeWord's training notebook on the setup page (`configs/remote/voice_wakeword.onnx`; the server checks that it takes openWakeWord features).
    - The WebSocket only accepts this page's own origin and at most 3 phones at once. It says the word was heard at most once every 2 s.
  - **Picovoice Porcupine** (optional; its sign-up doesn't accept Gmail-type addresses). It runs on the phone itself (`web/vendor/`, Apache-2.0, versions pinned in the file names). It needs a free Picovoice AccessKey, saved on the setup page in `configs/remote/voice.json` (mode 600). The page gets the key because Porcupine runs in the browser, and Porcupine checks it with `rest.picovoice.ai`.
    - Ready-made words: Jarvis, Computer, Alexa, Bumblebee, Terminator and a few more. Your own word: train it at console.picovoice.ai for **Web (WASM)** and upload the `.ppn` (`configs/remote/voice_keyword.ppn`).
  - One sensitivity slider serves both: Porcupine's sensitivity, or openWakeWord's score threshold (1 − sensitivity, between 0.2 and 0.9).
  - While a question is asked, the wake word lets go of the microphone so speech recognition can use it.
- **The answer is spoken** by Piper on this computer (`server/tts.py`) and played through the page's own audio, which the first tap unlocked. Android Chrome's built-in speech wouldn't start after a wake word (no tap just before), which is why. Voices: English (US: hfc_male, UK: alan), Português (Brasil), Nederlands, Español (rhasspy/piper-voices v1.0.0 "medium", downloaded and hash-checked when the image is built, about 315 MB). If the computer voice fails, the phone's own voice takes over; Settings › Voice › Voice can pick the phone's voice instead.
- **On screen**: a slim bar slides in at the top while listening, thinking and speaking, with your question and Claude's answer (three lines, scrolls if longer). The page stays visible and usable under it. ✕ stops; the mic asks again.
- **The question** is turned into text by Chrome's speech recognition, which sends the audio to Google. Language is set per phone (English, Português, Nederlands, Español).
- **The answer** comes from Claude Code inside the container (`server/voice.py`), signed in with the server's own Claude login in `configs/remote/claude` (the same one as the quota strip), so it counts against your plan.
  - It runs in an empty folder with web search as its only tool (no files, shell or web fetch; email and calendar only for a linked phone, see below), one question at a time, at most 150 a day (`REMOTE_VOICE_DAILY`), model `sonnet` (`REMOTE_VOICE_MODEL`).
  - The prompt asks for one to three spoken sentences and gives the local time. The last few questions and answers (within 5 minutes) go along, so follow-ups work. When Claude asks something back, the phone listens again without the wake word.
  - Simple questions take about 2 to 3 s; ones that need a web search take 15 to 20 s.
  - The server renews the login before each question (`Quota.ensure_login`), so Claude Code and the quota strip never renew it at the same time.
  - This needs the full Claude Code login. A sign-in made from the phone's quota sheet only reads usage; if voice says it isn't signed in, run `docker-compose exec -it remote claude auth login` in home-dashboard.
- Music pauses while you talk and plays again after (can be turned off).

### Email and calendar (linked phone only)

A linked phone can ask about your Gmail and Google Calendar by voice, with full access: read, search, send, reply, forward, trash, label, and create, change or delete events.

- **Where the access comes from**: the Gmail and Google Calendar connectors of the Claude account behind the server's login (claude.ai › Settings › Connectors). They load in Claude Code because that login has the `user:mcp_servers` scope. Only `mcp__claude_ai_Gmail` and `mcp__claude_ai_Google_Calendar` are allowed; Google Drive and Claude Docs are kept out (`--disallowedTools`), and `-p` mode refuses any other tool. `MCP_CONNECTION_NONBLOCKING=false` makes Claude Code wait for the connectors before it answers. Questions take about 5 s.
- **Who gets it**: only a phone linked with a code from this computer. Every other device on the network keeps the plain assistant (web search only, `--strict-mcp-config`).
  1. On the computer: `python3 /home/rafael/Documents/claude/remote/hooks/link_phone.py`. It prints a code like `ABCD-2345`, good for 10 minutes and 5 tries. Making it needs the hook key file, which only this computer has.
  2. On the phone, on the HTTPS address: Settings › Wake word and voice › Email and calendar: type the code, tap Link.
  3. The phone gets a random key in a cookie (`remote_phone`: HttpOnly, Secure, SameSite=Strict, path `/api`, 10 years). The server keeps only its SHA-256 hash, in `configs/remote/phone_keys.json` (mode 600, at most 5 phones). The key counts only over HTTPS.
  - Unlink: "Unlink this phone" in the same place, or `python3 hooks/link_phone.py --unlink-all` for every phone.
- **Safety**: the prompt asks Claude to say what it will do and get a spoken yes before it sends, deletes or changes anything, to read email aloud as a short gist, and to treat text inside emails and events as data, never as instructions. This is a request to the model, not a hard block: an email written to trick it (prompt injection) could still get it to act. Unlink the phone, or remove the connectors from the Claude account, to take the access away.

### HTTPS (for the microphone)

Phones only give a page the microphone over HTTPS, so the server also listens on **8797** with its own certificate (`server/tls.py`). Plain http on 8796 stays for the hooks and everything else.

- On first start the server makes a small certificate authority in `configs/remote/tls` (`ca.key` mode 600), limited by X.509 name constraints to 192.168.1.163, 127.0.0.1 and localhost. A phone that trusts it still refuses its certificates for any other site.
- The server certificate lasts 397 days and is renewed on start when fewer than 30 days are left. If `REMOTE_URL`'s address changes, a new authority is made and the phone installs it again.
- Install it on the phone once (setup page, step 3): open `http://192.168.1.163:8796/ca.crt`, name it Remote, choose **VPN and apps**. Android may ask for a screen lock first. Then open `https://192.168.1.163:8797/` and add it to the home screen.

## Files

- `server/app.py` - routes, the host check, the hook endpoints and the Spotify routes
- `server/state.py` - sessions and pending permission requests (`state.json` in the data folder)
- `server/spotify.py` - Spotify Web API client
- `server/lyrics.py` - lyrics from LRCLIB
- `server/quota.py` - Claude plan quotas, read with the server's own Claude sign-in
- `server/voice.py` - voice questions (Claude Code) and the wake-word settings
- `server/phonekey.py` - phone link codes and keys (email and calendar for voice)
- `server/wakeword.py` - openWakeWord on the server
- `server/trainer.py` - trains new openWakeWord wake words from the phone
- `server/tls.py` - the HTTPS certificates
- `web/voice.js` - wake word, listening, speaking; `web/wake-worklet.js` - mic to 16 kHz for openWakeWord; `web/vendor/` - Porcupine
- `tests/fixtures/` - "hey jarvis" and another sentence, spoken by Piper TTS, for the wake word tests
- `web/` - the phone page (`index.html`, `app.css`, `app.js`) and the setup page (`setup.html`, `setup.js`), which works on the phone and the computer
- `hooks/claude_hook.py` - the Claude Code hook
- `hooks/link_phone.py` - makes a code to link a phone for email and calendar
- `tests/` - API tests, Spotify tests (mocked), and browser tests at 640×360 and 360×640 (screenshots in `tests/shots/`)

## Running

The server is the `remote` service in **`/home/rafael/Documents/home-dashboard/docker-compose.yml`** (the home-dashboard compose stack, next to daily-hub and gym). It builds this repository's `Dockerfile` (target `runtime`, which includes Claude Code for voice), publishes ports 8796 (http) and 8797 (https) and restarts by itself (`unless-stopped`).

Run it only from there. This repository's own `compose.yaml` is for the tests, not for running the server. A second copy on port 8796 would take the port from `remote` and use a different hook key.

```bash
cd /home/rafael/Documents/home-dashboard
docker-compose up -d --build remote      # first start, or after any change here
docker logs -f remote
```

- Data (hook key `token`, `state.json`, `spotify.json`, the quota sign-in `claude/`, linked phones `phone_keys.json`, trained wake words `wakewords/`) lives in `home-dashboard/configs/remote`, not in this repository.
- `REMOTE_URL` (`http://192.168.1.163:8796`) is the phone's address: the setup page's QR code shows it, and only this host name (plus 127.0.0.1 and localhost) is answered. Change it if the computer's LAN address changes (a DHCP reservation on the router keeps it fixed).

### Access

There is no sign-in or pairing: any device on the home network can open `http://192.168.1.163:8796/` and use it, Allow and Deny included (the user's choice). The setup page has a QR code of that address for the phone. What stays protected:

- Hook calls (`/hook/*`) need the key in `configs/remote/token`, which `hooks/claude_hook.py` reads from the file. Without it, nothing else on the network can post fake sessions or prompts. Delete the file and restart to make a new key.
- The server answers only requests addressed to its own LAN address (from `REMOTE_URL`), `127.0.0.1` or `localhost`, so a web page can't reach it through DNS rebinding. Changes need an `X-Remote: 1` header, which other sites can't send to it.

### Claude quotas (once)

The quota strip at the bottom of the Claude panel shows the same numbers as `/usage` in Claude Code:
- the current session (5-hour) limit,
- the weekly limit for all models,
- the most-used per-model weekly limit.

Each bar shows how much is used and when it resets. It turns amber from 75% and red from 90%. Tap it for every limit, the reset times, extra usage and a Refresh button.

The server reads them from Claude's usage service with **its own Claude sign-in**, made once **from the phone**:

1. Tap the quota strip ("Claude quotas · tap to sign in"), then **Sign in to Claude**. Claude's sign-in page opens in a new tab: sign in with the same account as on the computer.
2. Claude then shows a code. Copy it, go back to the Remote tab, paste it and tap **Connect**.

Details:
- This sign-in asks only for the `user:profile` permission. It can read the plan's usage but can't use Claude, even if its token leaked.
- It is used only by this server, so the server renews it itself without touching the desktop app's or the terminal's login.
- Its tokens stay in `configs/remote/claude/.credentials.json`; the page only gets percentages and times.
- The server asks the usage service at most every 3 minutes.
- If the sign-in ends, the strip says so; sign in again the same way.
- **Sign out** in the quota sheet deletes it.
- Running `CLAUDE_CONFIG_DIR=/home/rafael/Documents/home-dashboard/configs/remote/claude claude auth login` on the computer works too, but that login gets Claude Code's full permissions.

The usage service isn't a documented public API (it's what Claude Code's `/usage` calls), so a change on Claude's side can break it. The strip then shows the error instead of numbers.

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
