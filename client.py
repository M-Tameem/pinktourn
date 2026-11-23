"""
Pink Noise Anti-Cheat Client - TOURNAMENT EDITION v2.2
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
import queue
import argparse
from settings import SettingsStore
from datetime import datetime
import sys

ADMIN_PASSWORD = "pink123"
LOCK_TIMEOUT_SECONDS = 15

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
        self._device = None
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
            self._device = imm_device
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
            # GetAllSessions() only inspects the default output. Tournament pink
            # noise typically plays on a separate USB endpoint, so enumerate it.
            from pycaw.pycaw import IAudioSessionManager2, IAudioSessionControl2, AudioSession
            if self._device is None:
                return False
            my_pid = os.getpid()
            interface = self._device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            manager = cast(interface, POINTER(IAudioSessionManager2))
            enumerator = manager.GetSessionEnumerator()
            sessions = [AudioSession(enumerator.GetSession(i).QueryInterface(IAudioSessionControl2))
                        for i in range(enumerator.GetCount())]
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
        except Exception as exc:
            raise RuntimeError("Windows audio monitoring read failed") from exc

    def check_device_volume_changed(self):
        if not self.endpoint_volume:
            return False, None
        try:
            cur = self.endpoint_volume.GetMasterVolumeLevelScalar()
            return (abs(cur - self.baseline_device_volume) > 0.02, cur)
        except Exception as exc:
            raise RuntimeError("Windows audio monitoring read failed") from exc

    def check_session_muted(self):
        if not self.session_volume:
            return False, None
        try:
            cur = bool(self.session_volume.GetMute())
            return (cur != self.baseline_session_mute, cur)
        except Exception as exc:
            raise RuntimeError("Windows audio monitoring read failed") from exc

    def check_session_volume_changed(self):
        if not self.session_volume:
            return False, None
        try:
            cur = self.session_volume.GetMasterVolume()
            return (abs(cur - self.baseline_session_volume) > 0.02, cur)
        except Exception as exc:
            raise RuntimeError("Windows audio monitoring read failed") from exc

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
        # Stream lifecycle calls must never hold the lock used by the callback:
        # PortAudio may wait for that callback inside start(), stop() or close().
        self.stop_playback()
        if self.pink_noise_buffer is None:
            self.initialize_buffer()
        with self.lock:
            self.device_id = device_id
            self.volume = float(volume)
            self.current_position = 0
            self.last_callback_time = None
            self.is_playing = True
        stream = None
        try:
            stream = sd.OutputStream(
                device=device_id, channels=1, callback=self.audio_callback,
                samplerate=self.generator.sample_rate, blocksize=2048, dtype='float32',
            )
            with self.lock:
                self.stream = stream
            stream.start()
            log.info(f"Started playback on device {device_id}")
            return True
        except Exception as e:
            log.error(f"Failed to start playback: {e}")
            self.stop_playback()
            self.callback("playback_error", str(e))
            return False

    def stop_playback(self):
        with self.lock:
            stream = self.stream
            self.stream = None
            self.is_playing = False
            self.last_callback_time = None
        if stream:
            try:
                stream.stop()
            finally:
                stream.close()

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
            return False
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
        self._diagnostic = {}
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

        self.running = True
        ready = threading.Event()
        result = []

        def monitor_worker():
            import comtypes
            initialized = False
            try:
                comtypes.CoInitialize()
                initialized = True
                outcome = self.windows_monitor.initialize(device_id, device_name)
                result.append(outcome)
                ready.set()
                if outcome[0] and self.running:
                    self._monitor_loop()
            except Exception as exc:
                result.append((False, str(exc)))
                ready.set()
            finally:
                # Release COM interfaces on their owning thread.
                self.windows_monitor.endpoint_volume = None
                self.windows_monitor.session_volume = None
                self.windows_monitor._device = None
                if initialized:
                    comtypes.CoUninitialize()

        self.thread = threading.Thread(target=monitor_worker, daemon=True)
        self.thread.start()
        if not ready.wait(6):
            self.running = False
            return False, "Windows audio monitoring timed out. Restart the app before retrying."
        if not result[0][0]:
            self.running = False
        return result[0]

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
                with self._flag_lock:
                    self._diagnostic = self.windows_monitor.get_diagnostic_info()
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
                self._set_flag("monitoring_failed", str(e), "monitoring_failed")
                time.sleep(0.2)

    def get_tamper_status(self):
        with self._flag_lock:
            flags = self.tamper_flags.copy()
            details = self.tamper_details.copy()
        return any(flags.values()), flags, details

    def get_diagnostic_info(self):
        with self._flag_lock:
            return self._diagnostic.copy()


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
        self.first_response = threading.Event()
        self.last_error = "Never connected"

    def start(self):
        self.running = True
        self.last_success_time = None
        self.first_response.clear()
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
                        self.last_error = ""
                        self.first_response.set()
                    else:
                        self.last_error = f"Server returned HTTP {r.status_code}"
                        if r.status_code == 403:
                            self.first_response.set()
                        log.error(self.last_error)
                except requests.RequestException as e:
                    self.last_error = str(e)
                    log.warning(f"[Heartbeat] Failed ({self.server_url}/heartbeat): {e}")

            except Exception as e:
                log.error(f"[Heartbeat Loop] {e}")

            time.sleep(0.5)

    def get_connection_status(self):
        if self.last_success_time is None:
            return False, self.last_error or "Never connected"
        age = time.time() - self.last_success_time
        if age > 5:
            return False, f"No response for {age:.1f}s"
        return True, f"Connected ({age:.1f}s ago)"


# ============================================================
# GUI
# ============================================================
class AntiCheatClient(tk.Tk):
    def __init__(self, server_url=None):
        super().__init__()
        self.title("PinkTourn - Tournament Audio Monitor")
        self.geometry("760x700")
        self.minsize(720, 660)
        self.configure(background="#f3f5f8")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f3f5f8", foreground="#1e293b")
        style.configure("TButton", padding=(12, 7))
        style.configure("TEntry", padding=6, fieldbackground="white")
        style.configure("TCombobox", padding=6, fieldbackground="white")
        style.configure("TLabelframe", borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        style.configure("Accent.TButton", background="#2563eb", foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#cbd5e1"), ("active", "#1d4ed8")])
        style.configure("Muted.TLabel", foreground="#64748b")

        if not WINDOWS_AUDIO_AVAILABLE:
            messagebox.showwarning(
                "Warning",
                "pycaw library not installed!\n\n"
                "Windows audio monitoring will not work.\n"
                "Install with: pip install pycaw comtypes"
            )

        self.machine_id = generate_machine_id()
        self.is_locked = False
        self.admin_password = ADMIN_PASSWORD
        self.settings_store = SettingsStore()
        try:
            self.saved_settings = self.settings_store.load()
        except Exception as exc:
            messagebox.showerror("Settings could not be loaded",
                                 f"{self.settings_store.path}\n\n{exc}\n\n"
                                 "Restore the settings file before restarting the app.")
            self.destroy()
            raise RuntimeError("Cannot safely load client settings") from exc
        self._save_timer = None
        self._test_stop_timer = None
        self._job_thread = None
        self._job_cancel = threading.Event()
        self._job_results = queue.Queue()
        self._job_kind = None
        self._job_timed_out = False
        self._status_timer = None
        self.server_url = "http://localhost:8000"
        self.auth_token = ""
        self.selected_device_id = None
        self.selected_device_name = None
        self.current_volume = 0.5
        self.server_url = self.saved_settings.get("server_url", self.server_url)
        self.auth_token = self.saved_settings.get("auth_token", "")
        self.current_volume = max(0.0, min(1.0, float(self.saved_settings.get("volume", 0.5))))

        self.audio_player = AudioPlayer(self._on_audio_event)
        self.tamper_monitor = TamperMonitor(self._on_tamper_event)
        self.server_comm = None

        self._setup_ui()

        try:
            self._load_audio_devices()
        except Exception as e:
            log.error(f"[Device Load] {e}")

        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self._restore_settings()
        if server_url is not None and not self.saved_settings.get("lock_active"):
            self.server_entry.delete(0, tk.END)
            self.server_entry.insert(0, normalize_server_url(server_url))
            self._save_settings()
        for entry in (self.server_entry, self.token_entry):
            entry.bind("<KeyRelease>", self._schedule_save)
            entry.bind("<FocusOut>", self._schedule_save)
        self.device_combo.bind("<<ComboboxSelected>>", self._schedule_save)
        if self.saved_settings.get("lock_active"):
            self.after(0, self._recover_interrupted_lock)

    # ---- UI Setup ----
    def _setup_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        main = ttk.Frame(self, padding=24)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        ttk.Label(main, text="PinkTourn", font=("Segoe UI", 24, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(main, text="Tournament audio monitor", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 12))
        ttk.Label(main, text="Choose the output carrying pink noise. USB, 3.5 mm and wireless outputs can be selected.",
                  wraplength=650).grid(row=2, column=0, sticky="w", pady=(0, 8))
        text = "Windows audio monitoring available" if WINDOWS_AUDIO_AVAILABLE else "Windows audio monitoring unavailable - install pycaw"
        ttk.Label(main, text=text, foreground="#15803d" if WINDOWS_AUDIO_AVAILABLE else "#b45309").grid(
            row=3, column=0, sticky="w", pady=(0, 16))

        self.unlocked_frame = ttk.LabelFrame(main, text="Before the match", padding=18)
        self.unlocked_frame.grid(row=4, column=0, sticky="ew", pady=(0, 16))
        self.unlocked_frame.columnconfigure(1, weight=1)
        ttk.Label(self.unlocked_frame, text="Pink noise output").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.device_combo = ttk.Combobox(self.unlocked_frame, width=42, state="readonly")
        self.device_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(self.unlocked_frame, text="Noise volume").grid(row=1, column=0, sticky="w")
        self.volume_label = ttk.Label(self.unlocked_frame, text="50%", width=5, anchor="e")
        self.volume_label.grid(row=1, column=2, padx=(12, 0))
        self.volume_scale = ttk.Scale(self.unlocked_frame, from_=0, to=1,
                                      orient="horizontal", command=self._on_volume_change)
        self.volume_scale.set(self.current_volume)
        self.volume_scale.grid(row=1, column=1, sticky="ew", pady=8)
        buttons = ttk.Frame(self.unlocked_frame)
        buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 16))
        ttk.Button(buttons, text="Test audio (3 seconds)", command=self._test_audio).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Run diagnostic", command=self._run_diagnostic).pack(side="left")
        ttk.Label(self.unlocked_frame, text="Server address").grid(row=3, column=0, sticky="w")
        self.server_entry = ttk.Entry(self.unlocked_frame, width=42)
        self.server_entry.insert(0, self.server_url)
        self.server_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        self.use_auth = tk.BooleanVar(value=bool(self.auth_token))
        ttk.Checkbutton(self.unlocked_frame, text="Server requires a token", variable=self.use_auth,
                        command=self._toggle_auth).grid(row=4, column=0, sticky="w", pady=6)
        self.token_entry = ttk.Entry(self.unlocked_frame, show="*")
        self.token_entry.insert(0, self.auth_token)
        self.token_entry.grid(row=4, column=1, columnspan=2, sticky="ew", pady=6)
        if not self.use_auth.get():
            self.token_entry.grid_remove()
        ttk.Button(self.unlocked_frame, text="Save settings", command=self._save_settings_clicked).grid(
            row=5, column=0, sticky="w", pady=(6, 14))
        ttk.Button(self.unlocked_frame, text="Check server connection", command=self._check_server_connection).grid(
            row=5, column=1, columnspan=2, sticky="e", pady=(6, 14))
        self.lock_button = ttk.Button(self.unlocked_frame, text="Lock for match", style="Accent.TButton",
                                      command=self._lock_system)
        self.lock_button.grid(row=6, column=0, columnspan=3, sticky="ew")
        self.setup_status = ttk.Label(self.unlocked_frame, text="Settings save automatically. No password needed to lock.",
                                      style="Muted.TLabel", wraplength=600)
        self.setup_status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.locked_frame = ttk.LabelFrame(main, text="Match monitoring", padding=18)
        self.locked_frame.grid(row=5, column=0, sticky="nsew", pady=(0, 12))
        self.locked_frame.columnconfigure(0, weight=1)
        self.locked_frame.rowconfigure(1, weight=1)
        self.locked_frame.grid_remove()
        self.status_canvas = tk.Canvas(self.locked_frame, width=620, height=72, highlightthickness=0)
        self.status_canvas.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.status_circle = self.status_canvas.create_rectangle(0, 0, 2000, 72, fill="green", outline="green")
        self.status_text = self.status_canvas.create_text(310, 36, text="SYSTEM OK",
                                                         font=("Segoe UI", 16, "bold"), fill="white")
        self.status_details = scrolledtext.ScrolledText(self.locked_frame, height=15, width=64,
                                                         font=("Consolas", 10), wrap=tk.WORD,
                                                         background="white", relief="flat", padx=12, pady=12)
        self.status_details.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
        ttk.Button(self.locked_frame, text="Unlock after match", command=self._unlock_system).grid(row=2, column=0, sticky="e")
        ttk.Label(main, text=f"Machine ID: {self.machine_id}", style="Muted.TLabel").grid(
            row=6, column=0, sticky="w", pady=(8, 0))

    def _toggle_auth(self):
        if self.use_auth.get():
            self.token_entry.grid()
        else:
            self.token_entry.delete(0, tk.END)
            self.token_entry.grid_remove()
        self._schedule_save()

    def _restore_settings(self):
        saved_device = self.saved_settings.get("device_name")
        if saved_device:
            choices = self.device_combo["values"]
            match = next((item for item in choices if item.split(": ", 1)[1] == saved_device), None)
            self.device_combo.set(match or "")
            if not match:
                self.setup_status.config(text=f"Saved device unavailable: {saved_device}. Select an output.")

    def _schedule_save(self, event=None):
        if not hasattr(self, "settings_store") or not hasattr(self, "device_combo"):
            return
        if self._save_timer:
            self.after_cancel(self._save_timer)
        self._save_timer = self.after(400, self._autosave)

    def _autosave(self):
        self._save_timer = None
        try:
            self._save_settings()
        except Exception as exc:
            log.error("Settings save failed: %s", exc)
            self.setup_status.config(text=f"Settings could not be saved: {exc}")

    def _save_settings_clicked(self):
        try:
            self._save_settings()
            self.setup_status.config(text="Settings saved.")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def _save_settings(self, lock_active=None):
        data = dict(self.saved_settings)
        choice = self.device_combo.get()
        data.update(server_url=normalize_server_url(self.server_entry.get()),
                    auth_token=self.token_entry.get().strip(), volume=self.current_volume)
        if choice:
            data["device_name"] = choice.split(": ", 1)[1]
        if lock_active is not None:
            data["lock_active"] = lock_active
        if lock_active is False:
            data["tamper_flags"] = {}
        elif self.is_locked:
            data["tamper_flags"] = self.tamper_monitor.get_tamper_status()[1]
        self.settings_store.save(data)
        self.saved_settings = data

    def _set_setup_enabled(self, enabled):
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Button, ttk.Entry, ttk.Scale, ttk.Combobox, ttk.Checkbutton)):
                    child.configure(state=("readonly" if isinstance(child, ttk.Combobox) else "normal") if enabled else "disabled")
                visit(child)
        visit(self.unlocked_frame)

    def _heartbeat_state(self):
        tampered, flags, _ = self.tamper_monitor.get_tamper_status()
        return dict(device_name=self.selected_device_name, locked_device=self.selected_device_name,
                    volume=self.current_volume, is_playing=self.audio_player.is_playing,
                    is_tampered=tampered, tamper_flags=flags)

    def _recover_interrupted_lock(self):
        self.selected_device_name = self.saved_settings.get("device_name", "Unavailable")
        with self.tamper_monitor._flag_lock:
            self.tamper_monitor.tamper_flags.update(self.saved_settings.get("tamper_flags", {}))
            self.tamper_monitor.tamper_flags["client_restarted"] = True
            self.tamper_monitor.tamper_details["client_restarted"] = (
                "Client restarted during a locked session. Admin must unlock before setup can change.")
        self.is_locked = True
        self.unlocked_frame.grid_remove()
        self.locked_frame.grid()
        self.attributes("-topmost", True)
        self.server_comm = ServerCommunicator(self.server_url, self.machine_id, self.auth_token)
        self.server_comm.update_state(self._heartbeat_state())
        self.server_comm.start()
        self._update_status()

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
        self._schedule_save()

    # ---- Test audio ----
    def _test_audio(self):
        choice = self.device_combo.get()
        if not choice:
            messagebox.showwarning("Warning", "Select an audio device first.")
            return
        if self._job_thread and self._job_thread.is_alive():
            return
        self._cancel_test_timer()
        self._begin_job("test", lambda: self.audio_player.start_playback(
            int(choice.split(":")[0]), self.current_volume))

    def _cancel_test_timer(self):
        if self._test_stop_timer:
            self.after_cancel(self._test_stop_timer)
            self._test_stop_timer = None

    def _stop_test_audio(self):
        self._test_stop_timer = None
        if not self.is_locked:
            self._begin_job("stop_test", self.audio_player.stop_playback)

    # ---- Diagnostic ----
    def _run_diagnostic(self):
        choice = self.device_combo.get()
        if not choice:
            messagebox.showwarning("Warning", "Select an audio device first.")
            return
        if self._job_thread and self._job_thread.is_alive():
            return
        device_id = int(choice.split(":")[0])
        device_name = choice.split(": ", 1)[1]
        def diagnose():
            import comtypes
            comtypes.CoInitialize()
            monitor = WindowsAudioMonitor()
            try:
                success, message = monitor.initialize(device_id, device_name)
                lines = [f"Selected device: {device_name}", message, ""]
                lines.extend(f"{key}: {value}" for key, value in monitor.get_diagnostic_info().items())
                if not monitor.session_volume:
                    lines.append("Session monitoring requires active pink noise on this device.")
                lines.append("Locking will also check playback and the server connection.")
                return "\n".join(lines)
            finally:
                monitor.endpoint_volume = None
                monitor.session_volume = None
                monitor._device = None
                comtypes.CoUninitialize()
        self._begin_job("diagnostic", diagnose)

    # ---- Lock / Unlock ----
    def _check_server_connection(self):
        if self.is_locked or (self._job_thread and self._job_thread.is_alive()):
            return
        try:
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Cannot save settings", str(exc))
            return
        url = normalize_server_url(self.server_entry.get())
        token = self.token_entry.get().strip()
        choice = self.device_combo.get()
        device_name = choice.split(": ", 1)[1] if choice else "Not selected"
        def check():
            communicator = ServerCommunicator(url, self.machine_id, token)
            communicator.update_state(dict(device_name=device_name, locked_device="",
                                           volume=self.current_volume,
                                           is_playing=self.audio_player.is_playing,
                                           is_tampered=False, tamper_flags={}))
            try:
                communicator.start()
                communicator.first_response.wait(5)
                connected, status = communicator.get_connection_status()
                if not connected:
                    raise RuntimeError(status)
                return f"Server accepted a heartbeat: {url}"
            finally:
                communicator.stop()
        self._begin_job("connection", check)

    def _begin_job(self, kind, operation):
        self._job_kind = kind
        self._job_cancel = threading.Event()
        self._job_timed_out = False
        self._job_started = time.monotonic()
        self._set_setup_enabled(False)
        self.setup_status.config(text=f"Working: {kind.replace('_', ' ')}...")
        cancel = self._job_cancel
        def work():
            try:
                result = operation()
                if cancel.is_set() and kind in ("test", "lock"):
                    if kind == "lock":
                        self._finish_unlock()
                    else:
                        self.audio_player.stop_playback()
                    raise RuntimeError("Operation cancelled after timeout.")
                self._job_results.put((True, result))
            except Exception as exc:
                log.exception("%s failed", kind)
                self._job_results.put((False, str(exc)))
        self._job_thread = threading.Thread(target=work, daemon=True)
        self._job_thread.start()
        self.after(50, self._poll_job)

    def _poll_job(self):
        if self._job_thread.is_alive():
            if time.monotonic() - self._job_started > LOCK_TIMEOUT_SECONDS and not self._job_timed_out:
                self._job_timed_out = True
                self._job_cancel.set()
                self.setup_status.config(text="Audio operation timed out. Waiting for the driver to return; restart the app if it stays stuck.")
                messagebox.showerror("Audio operation timed out",
                                     "The audio driver or Windows monitoring did not respond. "
                                     "The app has not completed this operation. "
                                     "Wait for cleanup or restart the app and check the selected device.")
            self.after(100, self._poll_job)
            return
        ok, result = self._job_results.get_nowait()
        kind = self._job_kind
        self._job_thread = None
        self._job_kind = None
        self._set_setup_enabled(True)
        if self._job_timed_out and ok and kind in ("test", "lock"):
            self._begin_job("lock_cleanup" if kind == "lock" else "stop_test", self._finish_unlock)
            return
        if kind == "lock_cleanup":
            if ok:
                self._save_settings(lock_active=False)
            else:
                messagebox.showerror("Cleanup failed", str(result))
            return
        if kind == "lock":
            if not ok or self._job_timed_out:
                try:
                    self._save_settings(lock_active=False)
                except Exception as exc:
                    log.error("Could not clear interrupted lock marker: %s", exc)
                self.setup_status.config(text=f"Lock failed: {result}")
                if self.tamper_monitor.thread and self.tamper_monitor.thread.is_alive():
                    self._set_setup_enabled(False)
                    self.setup_status.config(text="Windows monitoring is still stuck. Restart the app before retrying.")
                if not self._job_timed_out:
                    messagebox.showerror("Lock failed", str(result))
                return
            self.is_locked = True
            self.unlocked_frame.grid_remove()
            self.locked_frame.grid()
            self.attributes("-topmost", True)
            self.volume_scale.config(state="disabled")
            self._update_status()
        elif kind == "unlock":
            if not ok:
                messagebox.showerror("Unlock failed", str(result))
                return
            try:
                self._save_settings(lock_active=False)
            except Exception as exc:
                messagebox.showerror("Unlock failed", f"Could not save unlocked state: {exc}")
                return
            self.is_locked = False
            if self._status_timer:
                self.after_cancel(self._status_timer)
                self._status_timer = None
            self.locked_frame.grid_remove()
            self.unlocked_frame.grid()
            self.attributes("-topmost", False)
            self.setup_status.config(text="Unlocked. Settings saved.")
        elif kind == "connection":
            text = result if ok else f"Connection failed: {result}"
            self.setup_status.config(text=text)
            log.info("Connection check: %s", text)
        elif kind == "diagnostic":
            if ok and not self._job_timed_out:
                win = tk.Toplevel(self)
                win.title("System Diagnostic")
                text = scrolledtext.ScrolledText(win, width=85, height=24)
                text.pack(fill="both", expand=True, padx=10, pady=10)
                text.insert("1.0", result)
                text.configure(state="disabled")
            elif not ok:
                messagebox.showerror("Diagnostic failed", str(result))
        elif kind == "test":
            if ok and result and not self._job_timed_out:
                self.setup_status.config(text="Playing test noise for 3 seconds.")
                self._test_stop_timer = self.after(3000, self._stop_test_audio)
            elif not self._job_timed_out:
                messagebox.showerror("Audio test failed", str(result))
        elif not ok:
            messagebox.showerror("Audio operation failed", str(result))

    def _lock_system(self):
        if self.is_locked or (self._job_thread and self._job_thread.is_alive()):
            return
        choice = self.device_combo.get()
        if not choice:
            messagebox.showwarning("Warning", "Select an audio device.")
            return
        self._cancel_test_timer()
        self.server_url = normalize_server_url(self.server_entry.get())
        self.auth_token = self.token_entry.get().strip()
        self.selected_device_id = int(choice.split(":")[0])
        self.selected_device_name = choice.split(": ", 1)[1]
        try:
            # Write before starting so a crash during preparation is recoverable.
            self._save_settings(lock_active=True)
        except Exception as exc:
            messagebox.showerror("Cannot save settings", str(exc))
            return
        self._begin_job("lock", self._prepare_lock)

    def _prepare_lock(self):
        try:
            if not self.audio_player.start_playback(self.selected_device_id, self.current_volume):
                raise RuntimeError("Could not start pink noise playback.")
            deadline = time.monotonic() + 2
            while not self.audio_player.is_callback_healthy():
                if self._job_cancel.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("Audio callback did not start.")
                time.sleep(0.05)
            if self._job_cancel.is_set():
                raise RuntimeError("Lock cancelled.")
            success, message = self.tamper_monitor.start_monitoring(
                self.selected_device_id, self.selected_device_name, self.audio_player)
            if not success:
                raise RuntimeError(message)
            self.server_comm = ServerCommunicator(self.server_url, self.machine_id, self.auth_token)
            self.server_comm.update_state(self._heartbeat_state())
            self.server_comm.start()
            self.server_comm.first_response.wait(5)
            connected, status = self.server_comm.get_connection_status()
            if not connected:
                raise RuntimeError(f"Server did not accept a heartbeat: {status}")
            if self._job_cancel.is_set():
                raise RuntimeError("Lock cancelled.")
            return message
        except Exception:
            self.tamper_monitor.stop_monitoring()
            if self.server_comm:
                self.server_comm.stop()
            self.audio_player.stop_playback()
            raise

    def _unlock_system(self):
        if self._job_thread and self._job_thread.is_alive():
            return
        pw = simpledialog.askstring("Unlock", "Enter admin password:", show="*")
        if pw is None:
            return
        if pw != ADMIN_PASSWORD:
            messagebox.showerror("Error", "Incorrect password")
            return
        self._begin_job("unlock", self._finish_unlock)

    def _finish_unlock(self):
        self.tamper_monitor.stop_monitoring()
        if self.server_comm:
            self.server_comm.stop()
        self.audio_player.stop_playback()

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
        elif not srv_ok:
            self.status_canvas.itemconfig(self.status_circle, fill="orange", outline="darkorange")
            self.status_canvas.itemconfig(self.status_text, text="SERVER OFFLINE")
        else:
            self.status_canvas.itemconfig(self.status_circle, fill="green", outline="darkgreen")
            self.status_canvas.itemconfig(self.status_text, text="SYSTEM OK")

        # ASCII status text (no emoji — safe for all codepages and PyInstaller)
        L = []
        L.append("SYSTEM LOCKED - OUTPUT MONITORING")
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

        # Persist newly latched flags without writing on every UI refresh.
        if flags != self.saved_settings.get("tamper_flags", {}):
            try:
                self._save_settings(lock_active=True)
            except Exception as exc:
                log.error("Could not save tamper state: %s", exc)

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

        self._status_timer = self.after(250, self._update_status)

    # ---- Event handlers ----
    def _on_audio_event(self, event, data):
        log.info(f"[Audio] {event}: {data}")

    def _on_tamper_event(self, event, data):
        log.info(f"[Tamper] {event}: {data}")

    def destroy(self):
        for timer in self.tk.call("after", "info"):
            self.after_cancel(timer)
        super().destroy()

    def _on_close_attempt(self):
        if self.is_locked:
            messagebox.showwarning("Locked", "System is locked. Unlock first to exit.")
            return
        try:
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Settings could not be saved", str(exc))
            return
        self._cancel_test_timer()
        if self._job_thread and self._job_thread.is_alive():
            self._job_cancel.set()
        else:
            threading.Thread(target=self.audio_player.stop_playback, daemon=True).start()
        self.destroy()


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="PinkTourn tournament audio monitor")
    parser.add_argument("--server-url", help="Set and save the server address")
    parser.add_argument("--check-connection", action="store_true", help="Check the server after opening, without playing audio")
    args = parser.parse_args()
    log.info("Pink Noise Anti-Cheat Client v2.2 starting")
    log.info(f"Python {sys.version}, frozen={getattr(sys, 'frozen', False)}")
    log.info(f"Encoding: stdout={getattr(sys.stdout, 'encoding', 'N/A')}, fs={sys.getfilesystemencoding()}")
    app = AntiCheatClient(server_url=args.server_url)
    if args.check_connection:
        app.after(300, app._check_server_connection)
    app.mainloop()

if __name__ == "__main__":
    main()
