"""
Pink Noise Anti-Cheat Client - TOURNAMENT EDITION v2.1
Robust Windows monitoring with PyInstaller-safe operation.
Detects: device changes, device volume/mute, session volume/mute, process kill
"""
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext
import numpy as np
import sounddevice as sd
import threading
import time
import requests
import hashlib
import socket
import uuid
import os
import re
import io
import logging
from datetime import datetime
import sys

# ============================================================
# Safe Logging - survives PyInstaller --windowed (no console)
# ============================================================
def _get_log_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "anticheat.log")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "anticheat.log")

LOG_FILE = _get_log_path()

def _setup_logging():
    """Configure logging so nothing crashes when stdout is None (--windowed)."""
    logger = logging.getLogger("anticheat")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8", errors="replace")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    if sys.stdout is not None:
        try:
            sys.stdout.write("")
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            logger.addHandler(ch)
        except Exception:
            pass

    # Redirect bare print() so third-party libs don't crash on None stdout
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    return logger

log = _setup_logging()

# ============================================================
# Encoding Safety
# ============================================================
def safe_str(value):
    """Force a value to a clean UTF-8 string. Handles raw bytes from ctypes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return str(value)
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def sanitize_for_json(value):
    """Strip non-ASCII for safe HTTP/JSON transmission (no encoding surprises)."""
    s = safe_str(value)
    return s.encode("ascii", errors="replace").decode("ascii")


def normalize_server_url(raw):
    """
    Clean up whatever the player typed into the Server URL box.
      ' 192.168.1.100:8000 '         -> 'http://192.168.1.100:8000'
      'http:\\\\192.168.1.100:8000\\'  -> 'http://192.168.1.100:8000'
      'http://host:8000/heartbeat'   -> 'http://host:8000'
    """
    url = safe_str(raw).strip().strip('"').strip("'")
    url = url.replace("\\", "/")                      # Windows-style backslashes
    url = re.sub(r"^(https?):/*", r"\1://", url, flags=re.I)  # http:/x, http:///x
    if not re.match(r"^https?://", url, flags=re.I):
        # No scheme typed: LAN IPs / localhost get http, web addresses
        # (ngrok, trycloudflare, etc.) get https.
        bare = url.lstrip("/")
        host = re.split(r"[:/]", bare, 1)[0].lower()
        is_local = host == "localhost" or re.fullmatch(r"[\d.]+", host) or host.endswith(".local")
        url = ("http://" if is_local else "https://") + bare
    else:
        # Tunnel addresses typed with http:// -> use https (a redirect would
        # turn the POST into a GET and every heartbeat would fail).
        m = re.match(r"^http://([^:/]+)", url, flags=re.I)
        if m:
            host = m.group(1).lower()
            if host.endswith((".trycloudflare.com", ".ngrok-free.app", ".ngrok.app", ".ngrok.io", ".ngrok-free.dev", ".ngrok.dev")):
                url = "https://" + url[len("http://"):]
    url = url.rstrip("/")
    for suffix in ("/heartbeat", "/dashboard", "/docs"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url

# ============================================================
# Windows-specific audio monitoring
# ============================================================
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    WINDOWS_AUDIO_AVAILABLE = True
except ImportError:
    WINDOWS_AUDIO_AVAILABLE = False
    log.warning("pycaw not installed. Install with: pip install pycaw comtypes")

# ============================================================
# Windows Audio Monitor
# ============================================================
class WindowsAudioMonitor:
    """
    Monitors Windows audio for the SPECIFIC pink noise device:
    - Device-level: mute, volume (IAudioEndpointVolume)
    - Session-level: mute, volume (ISimpleAudioVolume / Volume Mixer)
    """
    def __init__(self):
        self.endpoint_volume = None
        self.baseline_device_volume = None
        self.baseline_device_mute = None

        self.session_volume = None
        self.baseline_session_volume = None
        self.baseline_session_mute = None

        self.locked_device_name = None
        self.locked_device_id = None
        self.is_available = WINDOWS_AUDIO_AVAILABLE

        self.device_match_method = None
        self.matched_windows_device_name = None

    def initialize(self, device_id, device_name):
        """Initialize monitoring. Returns (success, status_message)."""
        if not self.is_available:
            return False, "pycaw not installed"

        self.locked_device_name = device_name
        self.locked_device_id = device_id

        device_ok = self._initialize_device_monitoring(device_name)
        session_ok = self._initialize_session_monitoring()

        parts = []
        parts.append(f"Device: {'OK (' + self.device_match_method + ')' if device_ok else 'FAILED'}")
        parts.append(f"Session: {'OK' if session_ok else 'FAILED'}")

        if not device_ok and not session_ok:
            return False, "Both device and session monitoring failed"

        return True, " | ".join(parts)

    def _initialize_device_monitoring(self, device_name):
        try:
            devices = AudioUtilities.GetAllDevices()

            # Strategy 1: Exact match
            for device in devices:
                if device.FriendlyName and safe_str(device.FriendlyName) == device_name:
                    if self._try_activate_endpoint(device, "Exact match"):
                        return True

            # Strategy 2: Bidirectional contains
            for device in devices:
                if device.FriendlyName:
                    fname = safe_str(device.FriendlyName)
                    if device_name in fname or fname in device_name:
                        if self._try_activate_endpoint(device, "Substring match"):
                            return True

            # Strategy 3: Normalized match
            norm_target = self._normalize_device_name(device_name)
            for device in devices:
                if device.FriendlyName:
                    norm_cand = self._normalize_device_name(safe_str(device.FriendlyName))
                    if norm_target == norm_cand:
                        if self._try_activate_endpoint(device, "Normalized match"):
                            return True

            # Strategy 4: Token overlap (>= 50%)
            target_tokens = set(self._get_significant_tokens(device_name))
            for device in devices:
                if device.FriendlyName:
                    cand_tokens = set(self._get_significant_tokens(safe_str(device.FriendlyName)))
                    overlap = len(target_tokens & cand_tokens)
                    total = len(target_tokens | cand_tokens)
                    if total > 0 and overlap / total >= 0.5:
                        if self._try_activate_endpoint(device, f"Token match ({overlap}/{len(target_tokens)})"):
                            return True

            log.warning(f"Device monitoring: no match for '{device_name}'")
            return False

        except Exception as e:
            log.error(f"Device monitoring init error: {e}")
            return False

    def _try_activate_endpoint(self, device, match_method):
        try:
            from pycaw.pycaw import IMMDeviceEnumerator, IAudioEndpointVolume
            from comtypes import CoCreateInstance, CLSCTX_INPROC_SERVER, CLSCTX_ALL
            from ctypes import cast, POINTER

            # pycaw sometimes puts constants in a separate module depending on version
            try:
                from pycaw.constants import CLSID_MMDeviceEnumerator
            except ImportError:
                from pycaw.pycaw import CLSID_MMDeviceEnumerator

            # 1. Create the Windows COM device enumerator
            device_enumerator = CoCreateInstance(
                CLSID_MMDeviceEnumerator,
                IMMDeviceEnumerator,
                CLSCTX_INPROC_SERVER
            )

            # 2. Fetch the raw IMMDevice COM object using the string ID
            imm_device = device_enumerator.GetDevice(device.id)

            # 3. Activate the endpoint volume interface
            interface = imm_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)

            self.endpoint_volume = cast(interface, POINTER(IAudioEndpointVolume))
            self.baseline_device_volume = self.endpoint_volume.GetMasterVolumeLevelScalar()
            self.baseline_device_mute = bool(self.endpoint_volume.GetMute())
            self.device_match_method = match_method
            self.matched_windows_device_name = safe_str(device.FriendlyName)
            
            log.info(f"Device monitoring active: {self.matched_windows_device_name} ({match_method})")
            return True
            
        except Exception as e:
            log.warning(f"Endpoint activation failed for {safe_str(device.FriendlyName)}: {e}")
            return False
        
    def _initialize_session_monitoring(self):
        try:
            my_pid = os.getpid()
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.pid == my_pid:
                    self.session_volume = session.SimpleAudioVolume
                    self.baseline_session_volume = self.session_volume.GetMasterVolume()
                    self.baseline_session_mute = bool(self.session_volume.GetMute())
                    log.info(f"Session monitoring active: PID={my_pid}")
                    return True
            log.warning(f"Session monitoring: no audio session for PID {my_pid}")
            return False
        except Exception as e:
            log.error(f"Session monitoring init error: {e}")
            return False

    def _normalize_device_name(self, name):
        name = re.sub(r'\([^)]*\)', '', name)
        name = re.sub(r'[^\w\s]', '', name)
        return name.lower().strip()

    def _get_significant_tokens(self, name):
        name = re.sub(r'\([^)]*\)', '', name)
        words = re.findall(r'\w+', name.lower())
        ignore = {'audio', 'device', 'sound', 'the', 'a', 'an'}
        return [w for w in words if w not in ignore and len(w) > 1]

    def check_device_muted(self):
        if not self.endpoint_volume:
            return False, None
        try:
            cur = bool(self.endpoint_volume.GetMute())
            return (cur != self.baseline_device_mute, cur)
        except Exception:
            return False, None

    def check_device_volume_changed(self):
        if not self.endpoint_volume:
            return False, None
        try:
            cur = self.endpoint_volume.GetMasterVolumeLevelScalar()
            return (abs(cur - self.baseline_device_volume) > 0.02, cur)
        except Exception:
            return False, None

    def check_session_muted(self):
        if not self.session_volume:
            return False, None
        try:
            cur = bool(self.session_volume.GetMute())
            return (cur != self.baseline_session_mute, cur)
        except Exception:
            return False, None

    def check_session_volume_changed(self):
        if not self.session_volume:
            return False, None
        try:
            cur = self.session_volume.GetMasterVolume()
            return (abs(cur - self.baseline_session_volume) > 0.02, cur)
        except Exception:
            return False, None

    def get_diagnostic_info(self):
        info = {
            "is_available": self.is_available,
            "device_monitoring": self.endpoint_volume is not None,
            "session_monitoring": self.session_volume is not None,
            "locked_device_name": self.locked_device_name,
            "matched_device_name": self.matched_windows_device_name,
            "match_method": self.device_match_method,
        }
        if self.endpoint_volume:
            try:
                info["device_volume"] = f"{self.endpoint_volume.GetMasterVolumeLevelScalar():.0%}"
                info["device_mute"] = bool(self.endpoint_volume.GetMute())
                info["device_baseline_volume"] = f"{self.baseline_device_volume:.0%}"
                info["device_baseline_mute"] = self.baseline_device_mute
            except Exception:
                pass
        if self.session_volume:
            try:
                info["session_volume"] = f"{self.session_volume.GetMasterVolume():.0%}"
                info["session_mute"] = bool(self.session_volume.GetMute())
                info["session_baseline_volume"] = f"{self.baseline_session_volume:.0%}"
                info["session_baseline_mute"] = self.baseline_session_mute
            except Exception:
                pass
        return info

    @staticmethod
    def get_all_windows_devices():
        if not WINDOWS_AUDIO_AVAILABLE:
            return []
        try:
            devices = AudioUtilities.GetAllDevices()
            return [safe_str(d.FriendlyName) for d in devices if d.FriendlyName]
        except Exception as e:
            log.error(f"Error getting Windows devices: {e}")
            return []


# ============================================================
# Machine ID (SHA256 of hostname + MAC, stable across reboots)
# ============================================================
def generate_machine_id():
    hostname = socket.gethostname()
    mac = hex(uuid.getnode())[2:]
    base = f"{hostname}-{mac}"
    return hashlib.sha256(base.encode("utf-8", errors="replace")).hexdigest()[:16].upper()


# ============================================================
# Pink Noise Generator (fast spectral method)
# ============================================================
class PinkNoiseGenerator:
    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate

    def generate_pink_noise(self, duration_seconds=1.0):
        """Generate pink noise via spectral shaping. ~100x faster than per-sample loop."""
        num_samples = int(duration_seconds * self.sample_rate)
        rng = np.random.default_rng()
        white = rng.standard_normal(num_samples)

        fft = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(num_samples, d=1.0 / self.sample_rate)
        freqs[0] = 1.0  # avoid div by zero at DC
        fft *= 1.0 / np.sqrt(freqs)  # 1/sqrt(f) = pink spectrum

        pink = np.fft.irfft(fft, n=num_samples)

        max_abs = np.abs(pink).max()
        if max_abs > 0:
            pink = pink / max_abs * 0.7

        return pink.astype(np.float32)


# ============================================================
# Audio Player
# ============================================================
class AudioPlayer:
    def __init__(self, callback):
        self.callback = callback
        self.stream = None
        self.pink_noise_buffer = None
        self.current_position = 0
        self.is_playing = False
        self.device_id = None
        self.volume = 0.5
        self.generator = PinkNoiseGenerator()
        self.lock = threading.Lock()
        self.last_callback_time = None

    def initialize_buffer(self):
        self.pink_noise_buffer = self.generator.generate_pink_noise(5.0)

    def audio_callback(self, outdata, frames, time_info, status):
        if status:
            log.debug(f"Audio callback status: {status}")
        self.last_callback_time = time.time()

        with self.lock:
            if self.pink_noise_buffer is None or not self.is_playing:
                outdata.fill(0)
                return

            buf_len = len(self.pink_noise_buffer)
            start = self.current_position
            end = start + frames

            if end <= buf_len:
                chunk = self.pink_noise_buffer[start:end]
            else:
                chunk = np.concatenate((
                    self.pink_noise_buffer[start:buf_len],
                    self.pink_noise_buffer[0:end % buf_len]
                ))

            outdata[:, 0] = (chunk * self.volume).astype(np.float32)
            if outdata.shape[1] > 1:
                outdata[:, 1:] = 0

            self.current_position = (self.current_position + frames) % buf_len

    def start_playback(self, device_id, volume):
        with self.lock:
            if self.is_playing:
                self.stop_playback()

            self.device_id = device_id
            self.volume = float(volume)
            self.current_position = 0
            self.last_callback_time = time.time()

            if self.pink_noise_buffer is None:
                self.initialize_buffer()

            try:
                self.stream = sd.OutputStream(
                    device=device_id, channels=1,
                    callback=self.audio_callback,
                    samplerate=self.generator.sample_rate,
                    blocksize=2048, dtype='float32'
                )
                self.stream.start()
                self.is_playing = True
                log.info(f"Started playback on device {device_id}")
                return True
            except Exception as e:
                log.error(f"Failed to start playback: {e}")
                try:
                    self.callback("playback_error", str(e))
                except Exception:
                    pass
                return False

    def stop_playback(self):
        with self.lock:
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            self.is_playing = False
            self.last_callback_time = None

    def update_volume(self, volume):
        with self.lock:
            self.volume = float(volume)

    def get_status(self):
        with self.lock:
            return {
                "is_playing": self.is_playing,
                "device_id": self.device_id,
                "volume": self.volume,
                "callback_age": time.time() - self.last_callback_time if self.last_callback_time else None,
            }

    def is_callback_healthy(self):
        if not self.is_playing or self.last_callback_time is None:
            return True
        return (time.time() - self.last_callback_time) < 1.0


# ============================================================
# Tamper Monitor (thread-safe flags)
# ============================================================
class TamperMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.thread = None
        self.expected_device_id = None
        self.expected_device_name = None
        self.audio_player = None
        self.windows_monitor = WindowsAudioMonitor()

        self._flag_lock = threading.Lock()
        self.tamper_flags = {
            "device_unplugged": False,
            "device_muted": False,
            "device_volume_changed": False,
            "session_muted": False,
            "session_volume_changed": False,
            "playback_stopped": False,
            "callback_died": False,
        }
        self.tamper_details = {}

    def start_monitoring(self, device_id, device_name, audio_player):
        self.expected_device_id = device_id
        self.expected_device_name = device_name
        self.audio_player = audio_player

        # Fresh start for every lock: clear flags from the previous match and
        # drop any device handles/baselines left over from the last lock.
        with self._flag_lock:
            for k in self.tamper_flags:
                self.tamper_flags[k] = False
            self.tamper_details = {}
        self.windows_monitor = WindowsAudioMonitor()

        success, message = self.windows_monitor.initialize(device_id, device_name)
        if not success:
            return False, message

        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        return True, message

    def stop_monitoring(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

    def _set_flag(self, name, detail, cb_msg):
        with self._flag_lock:
            if not self.tamper_flags.get(name):
                self.tamper_flags[name] = True
                self.tamper_details[name] = detail
                try:
                    self.callback("tamper", cb_msg)
                except Exception:
                    pass

    def _monitor_loop(self):
        while self.running:
            try:
                # 1. Device still exists?
                try:
                    sd.query_devices(self.expected_device_id, 'output')
                except Exception:
                    self._set_flag("device_unplugged", "Device disconnected or removed", "device_unplugged")

                # 2. Device muted?
                changed, cur = self.windows_monitor.check_device_muted()
                if changed:
                    self._set_flag("device_muted", f"Device muted: {cur}", f"device_muted_{cur}")

                # 3. Device volume changed?
                changed, cur = self.windows_monitor.check_device_volume_changed()
                if changed:
                    bl = self.windows_monitor.baseline_device_volume
                    self._set_flag("device_volume_changed",
                                   f"Device volume: {bl:.0%} -> {cur:.0%}", "device_volume_changed")

                # 4. Session muted?
                changed, cur = self.windows_monitor.check_session_muted()
                if changed:
                    self._set_flag("session_muted", f"Volume Mixer muted: {cur}", f"session_muted_{cur}")

                # 5. Session volume changed?
                changed, cur = self.windows_monitor.check_session_volume_changed()
                if changed:
                    bl = self.windows_monitor.baseline_session_volume
                    self._set_flag("session_volume_changed",
                                   f"Volume Mixer volume: {bl:.0%} -> {cur:.0%}", "session_volume_changed")

                # 6. Playback active?
                if self.audio_player and not self.audio_player.is_playing:
                    self._set_flag("playback_stopped", "Audio playback stopped", "playback_stopped")

                # 7. Callback healthy?
                if self.audio_player and not self.audio_player.is_callback_healthy():
                    self._set_flag("callback_died", "Audio callback not responding", "callback_died")

                time.sleep(0.2)
            except Exception as e:
                log.error(f"[TamperMonitor] Error: {e}")
                time.sleep(1)

    def get_tamper_status(self):
        with self._flag_lock:
            flags = self.tamper_flags.copy()
            details = self.tamper_details.copy()
        return any(flags.values()), flags, details

    def get_diagnostic_info(self):
        return self.windows_monitor.get_diagnostic_info()


# ============================================================
# Server Communicator (ASCII-safe payloads, auth token)
# ============================================================
class ServerCommunicator:
    def __init__(self, server_url, machine_id, auth_token=""):
        self.server_url = normalize_server_url(server_url)
        self.machine_id = machine_id
        self.auth_token = auth_token
        # Ignore Windows proxy settings: this is a LAN server, and a system
        # proxy on a player's PC would otherwise swallow/mangle the request.
        self.session = requests.Session()
        self.session.trust_env = False
        self.running = False
        self.thread = None
        self._state_lock = threading.Lock()
        self.last_state = {}
        self.last_success_time = None

    def start(self):
        self.running = True
        self.last_success_time = time.time()
        self.thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

    def update_state(self, state):
        with self._state_lock:
            self.last_state = state

    def _heartbeat_loop(self):
        while self.running:
            try:
                with self._state_lock:
                    state = self.last_state.copy()

                if not state:
                    time.sleep(0.5)
                    continue

                # Build ASCII-safe payload — no emoji, no weird bytes
                payload = {
                    "machine_id": sanitize_for_json(self.machine_id),
                    "timestamp": datetime.now().isoformat(),
                    "device_name": sanitize_for_json(state.get("device_name", "")),
                    "locked_device": sanitize_for_json(state.get("locked_device", "")),
                    "volume": float(state.get("volume", 0)),
                    "is_playing": bool(state.get("is_playing", False)),
                    "is_tampered": bool(state.get("is_tampered", False)),
                    "tamper_flags": {
                        sanitize_for_json(k): bool(v)
                        for k, v in state.get("tamper_flags", {}).items()
                    },
                }

                headers = {"Content-Type": "application/json; charset=utf-8"}
                if self.auth_token:
                    headers["X-Auth-Token"] = self.auth_token

                try:
                    r = self.session.post(
                        f"{self.server_url}/heartbeat",
                        json=payload, headers=headers, timeout=1.5
                    )
                    if r.status_code == 200:
                        self.last_success_time = time.time()
                    elif r.status_code == 403:
                        log.error("[Heartbeat] Auth token rejected by server")
                except requests.RequestException as e:
                    log.warning(f"[Heartbeat] Failed ({self.server_url}/heartbeat): {e}")

            except Exception as e:
                log.error(f"[Heartbeat Loop] {e}")

            time.sleep(0.5)

    def get_connection_status(self):
        if self.last_success_time is None:
            return False, "Never connected"
        age = time.time() - self.last_success_time
        if age > 5:
            return False, f"No response for {age:.1f}s"
        return True, f"Connected ({age:.1f}s ago)"


# ============================================================
# GUI
# ============================================================
class AntiCheatClient(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pink Noise Anti-Cheat v2.1 [TOURNAMENT]")
        self.geometry("700x700")
        self.resizable(False, False)

        if not WINDOWS_AUDIO_AVAILABLE:
            messagebox.showwarning(
                "Warning",
                "pycaw library not installed!\n\n"
                "Windows audio monitoring will not work.\n"
                "Install with: pip install pycaw comtypes"
            )

        self.machine_id = generate_machine_id()
        self.is_locked = False
        self.admin_password = "admin123"
        self.server_url = "http://localhost:8000"
        self.auth_token = ""
        self.selected_device_id = None
        self.selected_device_name = None
        self.current_volume = 0.5

        self.audio_player = AudioPlayer(self._on_audio_event)
        self.tamper_monitor = TamperMonitor(self._on_tamper_event)
        self.server_comm = None

        self._setup_ui()

        try:
            self._load_audio_devices()
        except Exception as e:
            log.error(f"[Device Load] {e}")

        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

    # ---- UI Setup ----
    def _setup_ui(self):
        main = ttk.Frame(self, padding=12)
        main.grid(row=0, column=0, sticky="nsew")

        ttk.Label(main, text="Pink Noise Anti-Cheat v2.1",
                  font=("Arial", 14, "bold")).grid(row=0, column=0, columnspan=2, pady=10)

        win_text = "Windows Monitoring: AVAILABLE" if WINDOWS_AUDIO_AVAILABLE else "Windows Monitoring: UNAVAILABLE"
        win_color = "green" if WINDOWS_AUDIO_AVAILABLE else "orange"
        ttk.Label(main, text=win_text, foreground=win_color).grid(row=1, column=0, columnspan=2, pady=4)

        ttk.Label(main, text="USB headset: Pink noise (LOCKED)\n3.5mm: Game/Discord (free to adjust)",
                  foreground="blue", font=("Arial", 9)).grid(row=2, column=0, columnspan=2, pady=4)

        ttk.Label(main, text=f"Machine ID: {self.machine_id}",
                  font=("Arial", 8)).grid(row=3, column=0, columnspan=2, pady=4)

        # --- Unlocked frame ---
        self.unlocked_frame = ttk.LabelFrame(main, text="Configuration", padding=12)
        self.unlocked_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)

        ttk.Label(self.unlocked_frame, text="Pink Noise Output:").grid(row=0, column=0, sticky="w", pady=2)
        self.device_combo = ttk.Combobox(self.unlocked_frame, width=50, state="readonly")
        self.device_combo.grid(row=0, column=1, pady=4, columnspan=2)

        ttk.Label(self.unlocked_frame, text="Volume:").grid(row=1, column=0, sticky="w", pady=2)
        self.volume_label = ttk.Label(self.unlocked_frame, text="50%")
        self.volume_label.grid(row=1, column=2, padx=6)
        self.volume_scale = ttk.Scale(self.unlocked_frame, from_=0, to=1,
                                       orient="horizontal", command=self._on_volume_change)
        self.volume_scale.set(0.5)
        self.volume_scale.grid(row=1, column=1, pady=4, sticky="ew")

        btn_frame = ttk.Frame(self.unlocked_frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="Test Pink Noise (3s)", command=self._test_audio).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Run Diagnostic", command=self._run_diagnostic).pack(side="left", padx=5)

        ttk.Label(self.unlocked_frame, text="Server URL:").grid(row=3, column=0, sticky="w", pady=2)
        self.server_entry = ttk.Entry(self.unlocked_frame, width=50)
        self.server_entry.insert(0, self.server_url)
        self.server_entry.grid(row=3, column=1, pady=4, columnspan=2)

        ttk.Label(self.unlocked_frame, text="Auth Token:").grid(row=4, column=0, sticky="w", pady=2)
        self.token_entry = ttk.Entry(self.unlocked_frame, width=50)
        self.token_entry.grid(row=4, column=1, pady=4, columnspan=2)

        ttk.Label(self.unlocked_frame, text="Admin Password:").grid(row=5, column=0, sticky="w", pady=2)
        self.password_entry = ttk.Entry(self.unlocked_frame, width=50, show="*")
        self.password_entry.grid(row=5, column=1, pady=4, columnspan=2)

        ttk.Button(self.unlocked_frame, text="LOCK SYSTEM (Run Diagnostic First!)",
                   command=self._lock_system).grid(row=6, column=0, columnspan=3, pady=10, sticky="ew")

        # --- Locked frame ---
        self.locked_frame = ttk.LabelFrame(main, text="System Status", padding=15)
        self.locked_frame.grid(row=5, column=0, columnspan=2, sticky="nsew")
        self.locked_frame.grid_remove()

        self.status_canvas = tk.Canvas(self.locked_frame, width=200, height=200, bg="white")
        self.status_canvas.grid(row=0, column=0, pady=10)
        self.status_circle = self.status_canvas.create_oval(20, 20, 180, 180,
                                                             fill="green", outline="darkgreen", width=3)
        self.status_text = self.status_canvas.create_text(100, 100, text="SYSTEM OK",
                                                           font=("Arial", 16, "bold"), fill="white")

        self.status_details = scrolledtext.ScrolledText(self.locked_frame, height=15, width=70,
                                                         font=("Courier", 9), wrap=tk.WORD)
        self.status_details.grid(row=1, column=0, pady=10)

        ttk.Button(self.locked_frame, text="UNLOCK SYSTEM",
                   command=self._unlock_system).grid(row=2, column=0, pady=10)

    # ---- Device loading ----
    def _load_audio_devices(self):
        """
        Windows lists every device once per audio API (MME, DirectSound,
        WASAPI, WDM-KS), so one headset shows up 3-4 times. MME also cuts
        names off at 31 characters, which breaks matching against pycaw.
        Show only the DirectSound copy of each device: full names, any
        sample rate works. Fall back to the full list if that finds nothing.
        """
        devices = sd.query_devices()
        all_choices = []
        ds_choices = []
        seen = set()
        skip = ("primary sound driver", "microsoft sound mapper")
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] <= 0:
                continue
            name = safe_str(dev["name"])
            all_choices.append(f"{idx}: {name}")
            try:
                api = hostapis[dev["hostapi"]]["name"]
            except Exception:
                api = ""
            if "directsound" in api.lower() and not name.lower().startswith(skip) and name not in seen:
                seen.add(name)
                ds_choices.append(f"{idx}: {name}")
        choices = ds_choices or all_choices
        log.info(f"Audio devices listed: {len(choices)} (of {len(all_choices)} total)")
        self.device_combo["values"] = choices
        if choices:
            self.device_combo.current(0)

    def _on_volume_change(self, v):
        vol = float(v)
        self.current_volume = vol
        self.volume_label.config(text=f"{int(vol * 100)}%")
        if self.audio_player.is_playing and not self.is_locked:
            self.audio_player.update_volume(vol)

    # ---- Test audio ----
    def _test_audio(self):
        d = self.device_combo.get()
        if not d:
            messagebox.showwarning("Warning", "Select an audio device first.")
            return
        device_id = int(d.split(":")[0])
        self.audio_player.stop_playback()
        if self.audio_player.start_playback(device_id, self.current_volume):
            messagebox.showinfo("Test", "Pink noise playing for 3 seconds.")
            self.after(3000, self.audio_player.stop_playback)
        else:
            messagebox.showerror("Error", "Failed to start audio playback.")

    # ---- Diagnostic ----
    def _run_diagnostic(self):
        d = self.device_combo.get()
        if not d:
            messagebox.showwarning("Warning", "Select an audio device first.")
            return

        device_id = int(d.split(":")[0])
        device_name = d.split(": ", 1)[1]

        win = tk.Toplevel(self)
        win.title("System Diagnostic")
        win.geometry("700x500")
        text = scrolledtext.ScrolledText(win, width=80, height=30, font=("Courier", 9))
        text.pack(fill="both", expand=True, padx=10, pady=10)

        def out(line):
            text.insert(tk.END, line + "\n")
            text.see(tk.END)
            win.update()

        out("=" * 60)
        out("PINK NOISE ANTI-CHEAT DIAGNOSTIC")
        out("=" * 60)
        out("")
        out(f"SELECTED DEVICE:")
        out(f"  ID:   {device_id}")
        out(f"  Name: {device_name}")
        out("")

        out("ALL OUTPUT DEVICES (sounddevice):")
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_output_channels"] > 0:
                    out(f"  [{idx}] {safe_str(dev['name'])}")
        except Exception as e:
            out(f"  Error: {e}")
        out("")

        out("ALL WINDOWS DEVICES (pycaw):")
        wdevs = WindowsAudioMonitor.get_all_windows_devices()
        if wdevs:
            for idx, name in enumerate(wdevs):
                out(f"  [{idx}] {name}")
        else:
            out("  pycaw not available or no devices found")
        out("")

        out("TESTING MONITORING INITIALIZATION:")
        monitor = WindowsAudioMonitor()
        success, message = monitor.initialize(device_id, device_name)
        if success:
            out(f"  [OK] {message}")
            info = monitor.get_diagnostic_info()
            out("")
            out("  Details:")
            for k, v in info.items():
                out(f"    {k}: {v}")
        else:
            out(f"  [FAIL] {message}")
        if not monitor.session_volume and not self.audio_player.is_playing:
            out("  NOTE: 'Session: FAILED' is normal here. The Volume Mixer check")
            out("        only exists while pink noise is playing, and it starts")
            out("        automatically when you lock.")
        out("")
        out("=" * 60)

        if success:
            out("[OK] System ready to lock!")
        else:
            out("[WARN] Monitoring may be limited.")
        out("")

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)

    # ---- Lock / Unlock ----
    def _lock_system(self):
        d = self.device_combo.get()
        if not d:
            messagebox.showwarning("Warning", "Select an audio device.")
            return

        pw = self.password_entry.get()
        if not pw:
            messagebox.showwarning("Warning", "Enter an admin password.")
            return

        self.admin_password = pw
        self.server_url = normalize_server_url(self.server_entry.get())
        self.server_entry.delete(0, tk.END)
        self.server_entry.insert(0, self.server_url)   # show what will actually be used
        log.info(f"Server URL: {self.server_url}")
        self.auth_token = self.token_entry.get().strip()
        self.selected_device_id = int(d.split(":")[0])
        self.selected_device_name = d.split(": ", 1)[1]

        # 1. Start audio
        if not self.audio_player.start_playback(self.selected_device_id, self.current_volume):
            messagebox.showerror("Lock Failed", "Could not start pink noise playback.")
            return

        # 2. Wait for callback
        time.sleep(0.5)
        if not self.audio_player.is_callback_healthy():
            messagebox.showerror("Lock Failed", "Audio callback is not running properly.")
            self.audio_player.stop_playback()
            return

        # 3. Initialize monitoring
        success, message = self.tamper_monitor.start_monitoring(
            self.selected_device_id, self.selected_device_name, self.audio_player
        )
        if not success:
            messagebox.showerror(
                "Cannot Lock",
                f"Monitoring failed:\n{message}\n\n"
                "Check pycaw install and run diagnostic."
            )
            self.audio_player.stop_playback()
            return

        # 4. Start server comms
        self.server_comm = ServerCommunicator(self.server_url, self.machine_id, self.auth_token)
        self.server_comm.start()
        time.sleep(0.5)

        # Switch UI
        self.unlocked_frame.grid_remove()
        self.locked_frame.grid()
        self.is_locked = True
        self.attributes("-topmost", True)
        self.volume_scale.config(state="disabled")

        self._update_status()

        diag = self.tamper_monitor.get_diagnostic_info()
        connected, srv_status = self.server_comm.get_connection_status()
        summary = (
            f"System locked successfully.\n\n"
            f"Monitoring: {message}\n"
            f"Server: {srv_status}\n\n"
            f"Device monitoring: {'OK' if diag.get('device_monitoring') else 'N/A'}\n"
            f"Session monitoring: {'OK' if diag.get('session_monitoring') else 'N/A'}"
        )
        messagebox.showinfo("System Locked", summary)

    def _unlock_system(self):
        pw = simpledialog.askstring("Unlock", "Enter admin password:", show="*")
        if pw != self.admin_password:
            messagebox.showerror("Error", "Incorrect password")
            return

        self.tamper_monitor.stop_monitoring()
        if self.server_comm:
            self.server_comm.stop()
        self.audio_player.stop_playback()

        self.locked_frame.grid_remove()
        self.unlocked_frame.grid()
        self.is_locked = False
        self.attributes("-topmost", False)
        self.volume_scale.config(state="normal")
        messagebox.showinfo("Unlocked", "System unlocked.")

    # ---- Status loop ----
    def _update_status(self):
        if not self.is_locked:
            return

        audio = self.audio_player.get_status()
        is_tampered, flags, details = self.tamper_monitor.get_tamper_status()
        diag = self.tamper_monitor.get_diagnostic_info()
        srv_ok, srv_status = self.server_comm.get_connection_status() if self.server_comm else (False, "N/A")

        # Circle color
        if is_tampered:
            self.status_canvas.itemconfig(self.status_circle, fill="red", outline="darkred")
            self.status_canvas.itemconfig(self.status_text, text="TAMPERING")
        else:
            self.status_canvas.itemconfig(self.status_circle, fill="green", outline="darkgreen")
            self.status_canvas.itemconfig(self.status_text, text="SYSTEM OK")

        # ASCII status text (no emoji — safe for all codepages and PyInstaller)
        L = []
        L.append("SYSTEM LOCKED - USB HEADSET MONITORING")
        L.append("=" * 55)
        L.append("")
        L.append(f"Device: {self.selected_device_name}")
        if diag.get('matched_device_name'):
            L.append(f"Match:  {diag['matched_device_name']} ({diag.get('match_method', '?')})")
        L.append("")

        cb_age = audio.get('callback_age')
        cb_str = f"{cb_age:.1f}s ago" if cb_age is not None else "N/A"
        L.append(f"Audio Playback:    {'[OK]' if audio['is_playing'] else '[!!]'}  (callback: {cb_str})")
        L.append(f"Device Connected:  {'[OK]' if not flags.get('device_unplugged') else '[!!]'}")

        if diag.get('device_monitoring'):
            dv = diag.get('device_volume', '?')
            dm = '[!!] YES' if diag.get('device_mute') else '[OK] NO'
            dbv = diag.get('device_baseline_volume', '?')
            dbm = 'MUTED' if diag.get('device_baseline_mute') else 'UNMUTED'
            L.append(f"Device Mute:       {dm}  (baseline: {dbm})")
            L.append(f"Device Volume:     {dv}  (baseline: {dbv})")
        else:
            L.append("Device Monitor:    [--] unavailable")

        if diag.get('session_monitoring'):
            sv = diag.get('session_volume', '?')
            sm = '[!!] YES' if diag.get('session_mute') else '[OK] NO'
            sbv = diag.get('session_baseline_volume', '?')
            sbm = 'MUTED' if diag.get('session_baseline_mute') else 'UNMUTED'
            L.append(f"Session Mute:      {sm}  (baseline: {sbm})")
            L.append(f"Session Volume:    {sv}  (baseline: {sbv})")
        else:
            L.append("Session Monitor:   [--] unavailable")

        L.append(f"Server:            {'[OK]' if srv_ok else '[!!]'}  ({srv_status})")
        L.append("")
        L.append("=" * 55)

        if is_tampered:
            L.append("")
            L.append("!! TAMPERING DETECTED:")
            for key, val in details.items():
                if flags.get(key):
                    L.append(f"  * {val}")
        else:
            L.append("")
            L.append("[OK] No tampering detected")

        L.append("")
        L.append(f"Last check: {datetime.now().strftime('%H:%M:%S')}")

        self.status_details.delete(1.0, tk.END)
        self.status_details.insert(1.0, "\n".join(L))

        # Send to server
        if self.server_comm:
            self.server_comm.update_state({
                "device_name": self.selected_device_name,
                "locked_device": self.selected_device_name,
                "volume": audio["volume"],
                "is_playing": audio["is_playing"],
                "is_tampered": is_tampered,
                "tamper_flags": flags,
            })

        self.after(250, self._update_status)

    # ---- Event handlers ----
    def _on_audio_event(self, event, data):
        log.info(f"[Audio] {event}: {data}")

    def _on_tamper_event(self, event, data):
        log.info(f"[Tamper] {event}: {data}")

    def _on_close_attempt(self):
        if self.is_locked:
            messagebox.showwarning("Locked", "System is locked. Unlock first to exit.")
            return
        self.destroy()


# ============================================================
# MAIN
# ============================================================
def main():
    log.info("Pink Noise Anti-Cheat Client v2.1 starting")
    log.info(f"Python {sys.version}, frozen={getattr(sys, 'frozen', False)}")
    log.info(f"Encoding: stdout={getattr(sys.stdout, 'encoding', 'N/A')}, fs={sys.getfilesystemencoding()}")
    app = AntiCheatClient()
    app.mainloop()

if __name__ == "__main__":
    main()
