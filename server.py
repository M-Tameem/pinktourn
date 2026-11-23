"""
Pink Noise Anti-Cheat Server - TOURNAMENT EDITION v2.1
Real-time monitoring dashboard with auth and XSS protection.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
from datetime import datetime
import threading
import time
import html
import os
import uvicorn
from pathlib import Path

# ============================================================
# Configuration
# ============================================================
# Set via environment variable or leave empty to disable auth
AUTH_TOKEN = os.environ.get("ANTICHEAT_AUTH_TOKEN", "")
HEARTBEAT_TIMEOUT_SECONDS = 5  # a bit of slack for players connecting over the internet
STALE_CLIENT_HOURS = 4  # Remove clients not seen for this long

app = FastAPI(title="Pink Noise Anti-Cheat Server v2.1")

# CORS — allow dashboard from any origin on the LAN
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ============================================================
# Data Models
# ============================================================
class HeartbeatData(BaseModel):
    machine_id: str
    timestamp: str
    device_name: str
    locked_device: str
    volume: float
    is_playing: bool
    is_tampered: bool
    tamper_flags: Dict[str, bool]


def _sanitize(value: str) -> str:
    """HTML-escape a string to prevent XSS when rendered in the dashboard."""
    return html.escape(str(value), quote=True)


class ClientState:
    def __init__(self, machine_id: str):
        self.machine_id = _sanitize(machine_id)
        self.device_name = ""
        self.locked_device = ""
        self.volume = 0.0
        self.is_playing = False
        self.is_tampered = False
        self.tamper_flags: Dict[str, bool] = {}
        self.last_seen = datetime.now()
        self.is_online = True
        self.first_seen = datetime.now()

    def update(self, data: HeartbeatData):
        self.device_name = _sanitize(data.device_name)
        self.locked_device = _sanitize(data.locked_device)
        self.volume = data.volume
        self.is_playing = data.is_playing
        self.is_tampered = data.is_tampered
        # Sanitize flag keys too (they come from client)
        self.tamper_flags = {_sanitize(k): v for k, v in data.tamper_flags.items()}
        self.last_seen = datetime.now()
        self.is_online = True

    def to_dict(self):
        return {
            "machine_id": self.machine_id,
            "device_name": self.device_name,
            "locked_device": self.locked_device,
            "volume": round(self.volume * 100, 1),
            "is_playing": self.is_playing,
            "is_tampered": self.is_tampered,
            "tamper_flags": self.tamper_flags,
            "last_seen": self.last_seen.isoformat(),
            "is_online": self.is_online,
            "first_seen": self.first_seen.isoformat(),
        }


# ============================================================
# State Storage
# ============================================================
client_states: Dict[str, ClientState] = {}
state_lock = threading.Lock()

# ============================================================
# Auth Helper
# ============================================================
def _check_auth(request: Request):
    """Validate auth token if one is configured."""
    if not AUTH_TOKEN:
        return  # No auth configured
    token = request.headers.get("X-Auth-Token", "")
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid auth token")

# ============================================================
# Background: offline detection + stale cleanup
# ============================================================
def _background_cleanup():
    while True:
        time.sleep(2)
        now = datetime.now()
        with state_lock:
            stale_ids = []
            for cid, client in client_states.items():
                delta = (now - client.last_seen).total_seconds()
                # Mark offline after timeout
                if delta > HEARTBEAT_TIMEOUT_SECONDS and client.is_online:
                    client.is_online = False
                    client.is_tampered = True
                    if not client.tamper_flags.get("connection_lost"):
                        client.tamper_flags["connection_lost"] = True
                # Collect truly stale clients
                if delta > STALE_CLIENT_HOURS * 3600:
                    stale_ids.append(cid)
            # Remove stale
            for cid in stale_ids:
                del client_states[cid]

_cleanup_thread = threading.Thread(target=_background_cleanup, daemon=True)
_cleanup_thread.start()

# ============================================================
# Endpoints
# ============================================================
@app.post("/heartbeat")
async def receive_heartbeat(data: HeartbeatData, request: Request):
    _check_auth(request)
    mid = _sanitize(data.machine_id)
    with state_lock:
        if mid not in client_states:
            client_states[mid] = ClientState(data.machine_id)
            print(f"[NEW] {mid} - {_sanitize(data.device_name)}")
        client_states[mid].update(data)

        if data.is_tampered:
            active = [k for k, v in data.tamper_flags.items() if v]
            if active:
                print(f"[TAMPER] {mid} - {', '.join(active)}")

    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/states")
async def get_all_states():
    """Returns all client states + derived summary stats in one call."""
    with state_lock:
        clients = [c.to_dict() for c in client_states.values()]
    total = len(clients)
    online = sum(1 for c in clients if c["is_online"])
    tampered = sum(1 for c in clients if c["is_tampered"])
    ok = sum(1 for c in clients if c["is_online"] and not c["is_tampered"])
    return {
        "clients": clients,
        "stats": {
            "total": total,
            "online": online,
            "offline": total - online,
            "tampered": tampered,
            "ok": ok,
        },
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/state/{machine_id}")
async def get_client_state(machine_id: str):
    mid = _sanitize(machine_id)
    with state_lock:
        if mid not in client_states:
            raise HTTPException(status_code=404, detail="Client not found")
        return client_states[mid].to_dict()


# Keep /api/stats for backward compat but it's redundant now
@app.get("/api/stats")
async def get_stats():
    with state_lock:
        total = len(client_states)
        online = sum(1 for c in client_states.values() if c.is_online)
        tampered = sum(1 for c in client_states.values() if c.is_tampered)
        ok = sum(1 for c in client_states.values() if c.is_online and not c.is_tampered)
    return {
        "total_clients": total,
        "online": online,
        "offline": total - online,
        "tampered": tampered,
        "ok": ok,
        "timestamp": datetime.now().isoformat(),
    }


# ============================================================
# Dashboard
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_file = Path(__file__).parent / "dashboard.html"
    if html_file.exists():
        return FileResponse(html_file)
    return HTMLResponse(
        content="<h1>dashboard.html not found</h1><p>API at <a href='/docs'>/docs</a></p>",
        status_code=200,
    )


# ============================================================
# Runner
# ============================================================
def main():
    print("=" * 60)
    print("Pink Noise Anti-Cheat Server v2.1")
    print("=" * 60)
    print(f"\nDashboard: http://localhost:8000")
    print(f"API docs:  http://localhost:8000/docs")
    if AUTH_TOKEN:
        print(f"Auth:      ENABLED (token set via ANTICHEAT_AUTH_TOKEN)")
    else:
        print(f"Auth:      DISABLED (set ANTICHEAT_AUTH_TOKEN env var to enable)")
    print(f"Timeout:   {HEARTBEAT_TIMEOUT_SECONDS}s")
    print("=" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

if __name__ == "__main__":
    main()
