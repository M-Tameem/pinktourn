# Quick Setup Guide - PinkTourn v2.2

## For Tournament Admins

### 1. Server Setup (Your Machine)

```bash
pip install -r requirements.txt
python server.py
```

Dashboard opens at `http://localhost:8000`

**Optional: enable auth token** (recommended on shared networks):
```bash
# Linux/Mac
export ANTICHEAT_AUTH_TOKEN="my-secret-token"
python server.py

# Windows (cmd)
set ANTICHEAT_AUTH_TOKEN=my-secret-token
python server.py

# Windows (PowerShell)
$env:ANTICHEAT_AUTH_TOKEN="my-secret-token"
python server.py
```

If you set a token, players check **Server requires a token** and enter the same value.
Otherwise leave it unchecked. Use **Check server connection** before locking.

### 2. Client Distribution (Player Machines)

**Option A: Python Script**
```bash
pip install -r requirements.txt
python client.py
```

**Option B: EXE Distribution (Recommended)**
```bash
pip install pyinstaller
pyinstaller --onefile client.py
```

**Important:** Do NOT use `--windowed` with PyInstaller. The client handles
headless operation internally by redirecting output to `anticheat.log`.
If you want to hide the console window, use `--noconsole` instead and
verify the EXE works before distributing:

```bash
# Safe build (recommended)
pyinstaller --onefile client.py

# If you must hide the console (test first!)
pyinstaller --onefile --noconsole client.py
```

Distribute: `dist/client.exe` (players just run it, no Python needed).

A log file `anticheat.log` is created next to the EXE for troubleshooting.

Client settings are saved automatically at `%LOCALAPPDATA%\PinkTourn\settings.json`.
The same Windows user can restart or replace the EXE without re-entering the link,
token, selected output, or volume. A restart while locked requires admin unlock
with `pink123`, then locking again. It reports an interrupted session to the dashboard.

### 3. Pre-Tournament Checklist

**Server:**
- Server running on admin machine
- Dashboard accessible at http://<server-ip>:8000
- Firewall allows inbound TCP port 8000
- Auth token set (if using)

**Each Player:**
- Client running
- USB headset connected
- "Run Diagnostic" shows green
- Server URL set to http://<server-ip>:8000
- Auth token entered (if server requires it)
- Settings saved (server URL, token, output, volume)

### 4. Lock Procedure (Before Each Match)

**Player Side:**
1. Configure audio (USB = pink noise, 3.5mm = game)
2. Test pink noise at comfortable volume
3. Run diagnostic (verify OK)
4. Click "Lock System"
5. Confirm locked status

**Admin Side:**
1. Dashboard shows client card as green
2. No tamper flags
3. "Last seen: just now"

### 5. During Match

Watch the dashboard:
- Green card = OK
- Red pulsing card = TAMPERING (investigate immediately)
- Gray card = CLIENT OFFLINE (connection lost or killed)

### 6. After Match

Click Unlock and enter **`pink123`**. No password is required to lock.

---

## Network Setup (Multiple Machines)

**Server Machine:**
```bash
# Find your IP
ipconfig    # Windows
ip addr     # Linux

# Example: 192.168.1.100
# Dashboard: http://192.168.1.100:8000
```

**Client Machines:**
```
Server URL: http://192.168.1.100:8000
Auth Token: (same token set on server, if any)
```

**Firewall:** Allow inbound TCP port 8000 on the server machine.

---

## Players Not on the Same Network (Mac + Cloudflare Tunnel)

Run the server on your Mac and expose it with a free Cloudflare quick tunnel
(no account, no monthly request cap - unlike ngrok's free plan).

Terminal 1:
```bash
brew install cloudflared
pip3 install -r requirements.txt
export ANTICHEAT_AUTH_TOKEN="pick-something-long-and-random"
caffeinate -i python3 server.py
```

Terminal 2:
```bash
cloudflared tunnel --url http://localhost:8000
```

It prints an address like `https://something-random.trycloudflare.com`.
Give players that address (Server URL box) and the token (Auth Token box).
Typing just `something-random.trycloudflare.com` works too.

- **Always set the token** when the server is on the internet.
- Watch the dashboard at `http://localhost:8000` on the Mac, not via the tunnel.
- Keep both Terminal windows open. The tunnel lasts exactly as long as
  `cloudflared` runs; if it restarts you get a NEW address and players must
  unlock, paste it, and lock again.
- Quick tunnels allow 200 requests in flight at once; this app uses a tiny
  fraction of that even with dozens of players.

---

## Testing Detection

Before the tournament, verify each detection method:

1. **Volume Mixer Mute** - Open Volume Mixer, mute the client. Detect < 1s.
2. **Volume Mixer Volume** - Lower the client slider. Detect < 1s.
3. **Device Mute** - Mute USB device in Sound Settings. Detect < 1s.
4. **Force Close** - End process via Task Manager. Dashboard shows offline < 5s.
5. **Unplug** - Pull the USB headset. Screen should go red within ~1s.
6. **Re-lock** - Unlock, then lock again. Should start green (no leftover flags).

---

## Troubleshooting

**EXE crashes or shows garbled output:**
- Check `anticheat.log` next to the EXE for error details.
- Rebuild without `--windowed` flag.
- Ensure all dependencies are bundled (PyInstaller should handle this).

**Client can't connect to server:**
- Verify server URL (use IP, not `localhost`, for remote machines).
- Check firewall on server machine.
- If auth is enabled, verify the token matches.

**Diagnostic shows failures:**
- Device monitoring FAILED: Session monitoring may still work. Proceed.
- Session monitoring FAILED: Reinstall pycaw (`pip install --upgrade pycaw comtypes`).
- Both FAILED: Cannot lock. Fix installation.

---

## File Checklist

- `client.py` - Player application
- `server.py` - Admin server
- `dashboard.html` - Web dashboard (auto-served by server)
- `requirements.txt` - Python dependencies
- `README.md` - Full documentation
- `SETUP.md` - This quick guide
