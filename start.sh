#!/usr/bin/env bash
# Starts the pump backend and opens the controller UI.
#
#   bash start.sh            -> simulated Arduino on the TV (Termux); auto-detect on a laptop
#   bash start.sh mock       -> simulated Arduino (no hardware)
#   bash start.sh termux     -> REAL Arduino over USB from Termux (Android TV / phone)
#   bash start.sh serial     -> REAL Arduino via a normal serial port (laptop)
#   bash start.sh probe      -> (Termux) show what USB devices Android can see
#
# All output is captured to logs/ so nothing is lost if the terminal closes.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
mkdir -p logs
RUN_LOG="logs/start_$(date +"%Y%m%d_%H%M%S").log"
PORT="${PUMP_PORT:-5005}"

log() { echo "[$(date +%T)] $*" | tee -a "$RUN_LOG"; }

is_termux() { [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *com.termux* ]]; }

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python

if is_termux; then DEFAULT_MODE=mock; else DEFAULT_MODE=auto; fi
MODE="${1:-${PUMP_TRANSPORT:-$DEFAULT_MODE}}"
if [ "$MODE" = "auto" ] && is_termux; then MODE=termux; fi

# ---------------- Termux USB helpers ----------------

need_termux_usb() {
  if ! command -v termux-usb >/dev/null 2>&1; then
    echo ""
    echo "termux-usb isn't installed. One-time setup:"
    echo "  1. Install the 'Termux:API' app (same source you got Termux from, e.g. F-Droid)"
    echo "  2. In Termux run:   pkg install termux-api libusb python"
    echo "Then run this again."
    exit 1
  fi
}

usb_devices() {
  termux-usb -l 2>/dev/null | $PY -c '
import sys, json
try:
    for d in json.load(sys.stdin): print(d)
except Exception:
    pass'
}

make_wrapper() {   # $1 = file to write, $2 = the command it should exec
  printf '#!%s/bin/bash\nexec %s "$@"\n' "$PREFIX" "$2" > "$1"
  chmod +x "$1"
}

probe_device() {   # $1 = usb device path
  make_wrapper "$DIR/.usb_probe.sh" "$PY $DIR/usb_transport.py --probe"
  termux-usb -r -e "$DIR/.usb_probe.sh" "$1" 2>&1
}

pick_usb_device() {
  if [ -n "${PUMP_USB_DEVICE:-}" ]; then echo "$PUMP_USB_DEVICE"; return; fi
  mapfile -t DEVS < <(usb_devices)
  if [ "${#DEVS[@]}" -eq 0 ]; then
    echo "" >&2
    echo "Android reports no USB devices. Check: OTG/USB cable, Arduino plugged in," >&2
    echo "and that nothing else (like ArduinoDroid) is holding it." >&2
    return 1
  fi
  if [ "${#DEVS[@]}" -eq 1 ]; then echo "${DEVS[0]}"; return; fi

  echo "Several USB devices found — checking which one is the Arduino..." >&2
  for d in "${DEVS[@]}"; do
    echo "  probing $d (tap OK if Android asks)" >&2
    if probe_device "$d" | grep -q "ARDUINO=yes"; then echo "$d"; return; fi
  done
  echo "Couldn't tell automatically. Devices:" >&2
  i=1; for d in "${DEVS[@]}"; do echo "  $i) $d" >&2; i=$((i+1)); done
  read -r -p "Which number is the Arduino? " n
  echo "${DEVS[$((n-1))]}"
}

# ---------------- probe mode ----------------

if [ "$MODE" = "probe" ]; then
  need_termux_usb
  mapfile -t DEVS < <(usb_devices)
  [ "${#DEVS[@]}" -eq 0 ] && { echo "No USB devices visible to Termux."; exit 1; }
  for d in "${DEVS[@]}"; do
    echo "=== $d ==="
    probe_device "$d"
    echo ""
  done
  exit 0
fi

# ---------------- launch ----------------

export PUMP_TRANSPORT="$MODE"
log "Transport mode: $MODE"

if [ "$MODE" = "termux" ]; then
  need_termux_usb
  DEV="$(pick_usb_device)" || exit 1
  log "Using USB device: $DEV"
  log ">>> If Android shows a USB permission popup, tap OK <<<"
  make_wrapper "$DIR/.usb_launch.sh" "$PY $DIR/backend.py"
  termux-usb -r -e "$DIR/.usb_launch.sh" "$DEV" >> "$RUN_LOG" 2>&1 &
else
  $PY backend.py >> "$RUN_LOG" 2>&1 &
fi
BACKEND_PID=$!
log "Backend started (PID $BACKEND_PID)"

# Stop everything on exit/Ctrl+C. (In termux mode the real server is a child
# of termux-usb, so kill by name too — otherwise it keeps port $PORT busy.)
cleanup() {
  log "Stopping backend..."
  kill "$BACKEND_PID" 2>/dev/null
  pkill -f "backend.py" 2>/dev/null
}
trap cleanup EXIT

log "Waiting for the backend to respond..."
UP=0
for i in $(seq 1 60); do
  if curl -s -o /dev/null --max-time 1 "http://localhost:${PORT}/hardware"; then UP=1; break; fi
  sleep 0.5
done

if [ "$UP" -ne 1 ]; then
  log "The backend did not start. Last lines of the log:"
  tail -n 15 "$RUN_LOG"
  exit 1
fi
log "Backend is up."

# Say in plain words what the Arduino link looks like
curl -s --max-time 3 "http://localhost:${PORT}/hardware" | $PY -c '
import sys, json
try:
    h = json.load(sys.stdin)
except Exception:
    sys.exit()
if h.get("mock"):
    print("  Arduino: SIMULATED (no hardware in use)")
elif h.get("connected"):
    print("  Arduino: CONNECTED  ->  " + str(h.get("detail")))
else:
    print("  Arduino: NOT FOUND  ->  " + str(h.get("error")))
' | tee -a "$RUN_LOG"

URL="http://localhost:${PORT}/"
log "Opening controller UI: $URL"
if command -v termux-open-url >/dev/null 2>&1; then
  termux-open-url "$URL" >/dev/null 2>&1
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1
elif command -v open >/dev/null 2>&1; then
  open "$URL"
else
  echo "Could not auto-open a browser — open this manually: $URL"
fi

echo ""
echo "Controller UI:   $URL"
echo "Test dashboard:  ${URL}test"
echo "Run log:         $RUN_LOG"
echo "Server log:      logs/server.log"
echo ""
echo "Press Ctrl+C to stop (not Ctrl+Z)."

wait "$BACKEND_PID"
