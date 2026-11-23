"""Atomic per-user client settings with Windows-protected authentication tokens."""
import base64
import ctypes
import json
import os
from pathlib import Path
import tempfile


def settings_path():
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return root / "PinkTourn" / "settings.json"


def _protect(data, decrypt=False):
    """Windows DPAPI binds the token to this Windows user on this machine."""
    if os.name != "nt":
        raise RuntimeError("Saving authentication tokens requires Windows DPAPI")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else settings_path()

    def load(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Settings must be a JSON object")
        if data.get("token_protected"):
            data["auth_token"] = _protect(base64.b64decode(data["token_protected"]), True).decode("utf-8")
        return data

    def save(self, data):
        saved = dict(data)
        token = saved.pop("auth_token", "")
        saved.pop("token_protected", None)
        if token:
            saved["token_protected"] = base64.b64encode(_protect(token.encode("utf-8"))).decode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(saved, output, indent=2, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
