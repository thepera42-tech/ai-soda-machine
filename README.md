# AI Soda Dispenser — setup for the TV *and* the laptop

One project, three ways to talk to the Arduino. Pick with a word:

| Command | What it does | Use it on |
|---|---|---|
| `bash start.sh` | Simulated Arduino (default on the TV) | TV while developing |
| `bash start.sh termux` | REAL Arduino over USB through Termux | TV, with the Uno plugged in |
| `start.bat` (or `bash start.sh serial`) | REAL Arduino via a normal serial port, auto-detected | Friend's laptop |
| `bash start.sh probe` | Lists what USB devices the TV can see | TV, when debugging |

The badge in the top-left of the kiosk (and the pill on `/test`) always says which one you're in:
**SIMULATED**, **ARDUINO CONNECTED (termux/serial)**, or **ARDUINO NOT FOUND — reason**.

---

## A. One-time: put the sketch on the Uno

Use `pump_controller_uno.ino` (the original `pump_controller.ino` is for a Mega).
Upload it with ArduinoDroid on the TV, or the Arduino IDE on a laptop (board: Arduino Uno).
Test: Serial Monitor at 9600 baud, send `FIRE 1 500 80` -> you should see `OK` then `DONE 1`.

Pins on the Uno: pumps 1-6 = pins 3,5,6,9,10,11 (speed control works),
pumps 7-9 = pins 2,4,7 (on/off only). Pumps go through MOSFET drivers with their own power
supply — never straight to an Arduino pin.

## B. TV (Termux)

One-time setup:
1. Install the **Termux:API** app (from the same place you got Termux, e.g. F-Droid).
2. In Termux: `pkg install python libusb termux-api`
3. In Termux: `pip install flask flask-cors`

Every time:
1. Close ArduinoDroid (only one app can hold the Uno at a time).
2. Plug in the Uno, then run `bash start.sh termux`.
3. When Android shows a **USB permission popup, tap OK**.
4. Check the badge: it should say ARDUINO CONNECTED (termux).

Developing without the Uno? Just `bash start.sh` (simulated).

## C. Friend's laptop (Windows / Mac / Linux)

One-time: install Python (Windows: tick "Add Python to PATH"), then in this folder:
`pip install -r requirements.txt`

Every time: plug in the Uno, then run `start.bat` (Windows) or `bash start.sh` (Mac/Linux).
It finds the Arduino's port by itself. To force one: `set PUMP_SERIAL_PORT=COM5`
(Windows) or `export PUMP_SERIAL_PORT=/dev/ttyACM0`.
Cheap Uno clones on Windows may need the **CH340 driver**.

Want to open the kiosk on a different device (e.g. the TV) while the laptop runs the
backend? Start with `PUMP_HOST=0.0.0.0` set, then browse to `http://<laptop-ip>:5005/`.

## D. AI drink generation (Groq + ElevenLabs)

Create API keys in the [Groq console](https://console.groq.com/keys) and [ElevenLabs](https://elevenlabs.io/app/settings/api-keys).
Start the pump backend with `start.bat mock`, then enter both keys in `Settings > System`.
Keys stay in backend memory, are never saved in browser storage, and clear when the backend restarts.

Groq's `openai/gpt-oss-120b` model handles recipe reasoning; `openai/gpt-oss-20b` handles Coda's conversational text.
ElevenLabs `eleven_flash_v2_5` generates streamed audio and character timings for captions synchronized to the voice.
The default voice ID can be changed in `Settings > System`. Groq quotas vary by account; check the Groq limits page.
ElevenLabs currently lists 10,000 free credits per month, with text-to-speech charged per character.

Groq requests use system proxy settings by default. If a proxy is blocking them, set `GROQ_PROXY=direct` before
starting the backend to bypass environment proxies, or set `GROQ_PROXY=http://host:port` to use a specific proxy.
On Windows, run `set GROQ_PROXY=direct` in the same Command Prompt before `start.bat mock`.

`TALK` uses browser speech recognition for input. Replies use ElevenLabs audio, with captions following its timing data.
Voice input still requires browser support and microphone permission. Recipes use at most three ingredients, 12 seconds
per pump, and 24 total pump-seconds; the proposed recipe must be explicitly confirmed before any pour. Exact density
requires calibrated pump flow and ingredient density data, which are not currently configured.

---

## Files

- `backend.py` — server + pump logic; the Arduino link is pluggable (mock / serial / termux)
- `usb_transport.py` — the Termux USB driver (genuine Uno = CDC; CH340 clones best-effort)
- `kiosk.html`, `test_mode.html` — the two pages
- `start.sh`, `start.bat` — launchers · `requirements.txt` — laptop dependencies
- `pump_controller_uno.ino` (Uno) / `pump_controller.ino` (Mega)

## If something's wrong

- **ARDUINO NOT FOUND** — the badge/log says why. Logs: `logs/server.log`.
- **TV: no USB devices** — check the OTG cable/adapter; close ArduinoDroid; replug.
- **TV: "isn't a chip this backend can drive"** — run `bash start.sh probe` and share the output.
- **"Address already in use"** — an old copy is running: `pkill -9 -f backend.py`.
  Stop with **Ctrl+C**, never Ctrl+Z.
