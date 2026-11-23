# Pink Noise Anti-Cheat System v2.1 - Tournament Edition

## Overview

Monitors a USB headset playing pink noise during tournament matches. Detects any
attempt to mute, lower, or disconnect the pink noise device. Reports tamper events
to a central dashboard in real time.

### What Changed in v2.1

**Critical fixes:**
- Fixed a syntax error in `_lock_system()` that prevented the client from running at all.
- Fixed PyInstaller/EXE compatibility: the `--windowed` flag caused crashes because
  `sys.stdout` was `None` and every `print()` call threw an exception. All output now
  goes through a safe logging system that writes to `anticheat.log`.
- Fixed garbled HTTP requests in EXE builds: emoji characters and non-ASCII device names
  from Windows APIs caused encoding mismatches in the `requests` library. All payloads
  are now sanitized to ASCII before transmission.

**Security fixes:**
- Dashboard XSS: `machine_id` and `device_name` were injected into innerHTML
  unescaped. A crafted heartbeat could execute JS on the admin's browser. All values
  are now HTML-escaped server-side and client-side.
- Heartbeat authentication: optional shared token (`X-Auth-Token` header). Set
  `ANTICHEAT_AUTH_TOKEN` env var on the server; enter the same token in the client UI.
- CORS middleware added to the server.

**Code quality:**
- Removed dead `generate_machine_id()` standalone function (duplicate of the class method).
- Replaced bare `except:` clauses with `except Exception:`.
- Moved `import re` to module level (was re-imported inside methods on every call).
- Added `threading.Lock` around `tamper_flags` dict (was read/written from two threads
  without synchronization).
- Pink noise generation rewritten using spectral shaping (~100x faster than the
  per-sample Voss-McCartney loop; matters when PyInstaller slows down numpy startup).

**Dashboard:**
- Single API call per refresh (was two separate fetches for stats and states).
- DOM diffing: only rebuilds cards when data actually changes (no more flicker).
- Fixed `data-time` attributes so the secondary time-ago updater actually works.

---

## Installation

```bash
pip install -r requirements.txt
```

`pycaw` is required for Windows audio monitoring. Without it, the client will
warn on startup and refuse to lock.

### Files

- `client.py` - Client (runs on player machines)
- `server.py` - Server (runs on tournament admin machine)
- `dashboard.html` - Real-time web dashboard (served by server)

---

## Usage

### 1. Start the Server

```bash
# Without auth
python server.py

# With auth (recommended)
export ANTICHEAT_AUTH_TOKEN="your-secret"   # or `set` on Windows
python server.py
```

Dashboard: `http://localhost:8000`
API docs: `http://localhost:8000/docs`

### 2. Configure Clients

1. Launch `client.py`
2. Select USB headset device
3. Adjust volume
4. Click "Run Diagnostic" to verify monitoring works
5. Enter server URL (use the server's IP if on a different machine)
6. Enter auth token (if server requires one)
7. Set admin password

### 3. Lock System

1. Click "LOCK SYSTEM"
2. Pre-flight validation runs (audio, callback, monitoring, server)
3. If all pass, UI switches to locked mode
4. Window stays on top, volume slider disabled

### 4. During Match

Admin watches dashboard. Any tampering shows as a red pulsing card with details.
Client screen shows "TAMPERING" in red.

### 5. Unlock

Player enters admin password to unlock after the match.

---

## What Gets Detected

| Method                        | Detection Time | API Used                              |
|-------------------------------|----------------|---------------------------------------|
| Volume Mixer mute             | < 0.2s         | ISimpleAudioVolume.GetMute()          |
| Volume Mixer volume change    | < 0.2s         | ISimpleAudioVolume.GetMasterVolume()  |
| Device mute (Sound Settings)  | < 0.2s         | IAudioEndpointVolume.GetMute()        |
| Device volume (Sound Settings)| < 0.2s         | IAudioEndpointVolume.GetMasterVolumeLevelScalar() |
| USB device unplugged          | < 0.2s         | sounddevice.query_devices() exception |
| Process killed (Task Manager) | < 5s           | Server heartbeat timeout              |
| Audio callback dies           | < 1s           | Callback timestamp health check       |

**Not monitored (by design):** 3.5mm device changes, main Windows volume slider,
default device changes. Only the locked USB device is monitored.

---

## Building an EXE

```bash
pip install pyinstaller
pyinstaller --onefile client.py
```

**Do not use `--windowed`.** The client handles headless operation internally.
A log file `anticheat.log` is created next to the EXE.

If you must hide the console window:
```bash
pyinstaller --onefile --noconsole client.py
```
Test the resulting EXE before distributing.

---

## Security Notes

This is client-side monitoring. Determined cheaters can bypass it by modifying source,
using debuggers, or virtual audio cables. Mitigations for a one-night tournament:

- Staff physically present
- Fast detection (< 1s for most methods)
- Obvious visual alerts on dashboard
- Window forced on top when locked
- EXE distribution (harder to modify than .py)
- Auth token prevents spoofed heartbeats on the LAN

---

## Architecture

### Client -> Server Communication
- Protocol: HTTP POST to `/heartbeat`
- Frequency: every 500ms
- Payload: ASCII-safe JSON with full state + tamper flags
- Auth: optional `X-Auth-Token` header
- Timeout: 5s (server marks offline)

### Monitoring Loops
- Tamper check: 200ms (5/second)
- Heartbeat: 500ms
- Dashboard refresh: 500ms

### Windows API Integration

**Device-level** (IAudioEndpointVolume): mute state, volume scalar.
**Session-level** (ISimpleAudioVolume / Volume Mixer): per-app mute, per-app volume.
Baselines captured at lock time; any deviation > 2% triggers a tamper flag.

---

## Changelog from v2.0

- [FIX] Syntax error in `_lock_system()` (dangling `else:` block)
- [FIX] PyInstaller `--windowed` crash (stdout=None)
- [FIX] Garbled HTTP from emoji + non-ASCII device names in EXE builds
- [FIX] XSS via unsanitized machine_id/device_name in dashboard
- [FIX] Dashboard `data-time` attribute never set
- [FIX] Dead code: unused standalone `generate_machine_id()`
- [FIX] Bare `except:` clauses silently swallowing all exceptions
- [FIX] `import re` inside methods (re-imported every call)
- [FIX] Race condition on `tamper_flags` (no lock between monitor and GUI threads)
- [ADD] Auth token support (X-Auth-Token header)
- [ADD] CORS middleware on server
- [ADD] Stale client cleanup (removed after 4h)
- [ADD] Safe logging system (file + optional console)
- [ADD] Startup diagnostics logged (Python version, encoding, frozen state)
- [PERF] Spectral pink noise generation (~100x faster)
- [PERF] Dashboard: single API call, DOM diffing (no flicker)
- [PERF] Server: combined /api/states endpoint returns stats + client data
