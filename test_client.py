"""Regression checks; no physical audio playback or system volume changes."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
import client
from settings import SettingsStore


class SettingsTests(unittest.TestCase):
    def test_settings_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            data = dict(server_url="https://tournament.example", device_name="USB Headset",
                        volume=.37, lock_active=True, tamper_flags={"device_muted": True})
            SettingsStore(path).save(data)
            self.assertEqual(SettingsStore(path).load(), data)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_token_is_encrypted_and_recoverable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder) / "settings.json")
            store.save(dict(auth_token="private-test-token"))
            self.assertNotIn("private-test-token", store.path.read_text())
            self.assertEqual(store.load()["auth_token"], "private-test-token")

    def test_failed_replace_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder) / "settings.json")
            store.save(dict(volume=.5))
            with patch("settings.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    store.save(dict(volume=.1))
            self.assertEqual(store.load()["volume"], .5)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


class AudioTests(unittest.TestCase):
    def test_restart_and_stop_do_not_hold_callback_lock(self):
        player = client.AudioPlayer(Mock())
        player.pink_noise_buffer = np.ones(8192, dtype=np.float32)
        calls = []

        class Stream:
            def __init__(self, **kwargs):
                pass

            def start(self):
                # Simulate a driver that waits for the callback before returning.
                callback = threading.Thread(target=lambda: player.audio_callback(
                    np.zeros((2048, 1), dtype=np.float32), 2048, None, None), daemon=True)
                callback.start()
                callback.join(.5)
                if callback.is_alive():
                    raise RuntimeError("Callback blocked during start")
                calls.append("start")

            def stop(self):
                if not player.lock.acquire(timeout=.1):
                    raise RuntimeError("Callback lock held during stop")
                player.lock.release()
                calls.append("stop")

            def close(self):
                calls.append("close")

        result = []
        with patch.object(client.sd, "OutputStream", Stream):
            def exercise():
                result.extend([player.start_playback(1, .5), player.start_playback(1, .4)])
                player.stop_playback()
            worker = threading.Thread(target=exercise, daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Restart deadlocked")
        self.assertEqual(result, [True, True])
        self.assertEqual(calls.count("close"), 2)

    def test_no_callback_is_unhealthy(self):
        player = client.AudioPlayer(Mock())
        player.is_playing = True
        self.assertFalse(player.is_callback_healthy())

    def test_failed_monitor_read_does_not_report_healthy(self):
        monitor = client.WindowsAudioMonitor()
        monitor.endpoint_volume = Mock()
        monitor.endpoint_volume.GetMute.side_effect = RuntimeError("Endpoint invalid")
        with self.assertRaises(RuntimeError):
            monitor.check_device_muted()


class ConnectionTests(unittest.TestCase):
    def test_start_does_not_claim_connection(self):
        communicator = client.ServerCommunicator("localhost:8000", "probe")
        with patch.object(communicator, "_heartbeat_loop"):
            communicator.start()
            communicator.thread.join()
        self.assertFalse(communicator.get_connection_status()[0])

    def test_auth_rejection_is_reported(self):
        communicator = client.ServerCommunicator("localhost:8000", "probe")
        communicator.update_state({"is_playing": True})
        communicator.session.post = Mock(return_value=Mock(status_code=403))
        communicator.start()
        self.assertTrue(communicator.first_response.wait(1))
        communicator.stop()
        self.assertFalse(communicator.get_connection_status()[0])
        self.assertIn("403", communicator.get_connection_status()[1])


class ClientUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.temp.name) / "settings.json")
        self.patches = [
            patch.object(client, "SettingsStore", return_value=self.store),
            patch.object(client.AntiCheatClient, "_load_audio_devices", self.devices),
            patch.object(client.messagebox, "showwarning"),
            patch.object(client.messagebox, "showerror"),
            patch.object(client.messagebox, "showinfo"),
        ]
        for item in self.patches:
            item.start()
        self.app = None

    @staticmethod
    def devices(app):
        app.device_combo["values"] = ("7: USB Headset", "9: Other Device")
        app.device_combo.current(0)

    def open_app(self):
        self.app = client.AntiCheatClient()
        self.app.withdraw()
        return self.app

    def tearDown(self):
        if self.app:
            if self.app.server_comm:
                self.app.server_comm.stop()
            self.app.destroy()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_restore_link_volume_device_and_no_password_field(self):
        self.store.save(dict(server_url="https://saved.example", volume=.23, device_name="Other Device"))
        app = self.open_app()
        self.assertEqual(app.server_entry.get(), "https://saved.example")
        self.assertEqual(app.device_combo.get(), "9: Other Device")
        self.assertAlmostEqual(app.current_volume, .23)
        self.assertFalse(hasattr(app, "password_entry"))

    def test_missing_saved_device_is_not_silently_replaced(self):
        self.store.save(dict(device_name="Disconnected Headset"))
        self.assertEqual(self.open_app().device_combo.get(), "")

    def test_custom_port_is_saved_and_restored_on_restart(self):
        self.app = client.AntiCheatClient(server_url="http://127.0.0.1:8765")
        self.app.withdraw()
        self.assertEqual(self.app.server_entry.get(), "http://127.0.0.1:8765")
        self.app._on_close_attempt()
        self.app = None
        app = self.open_app()
        self.assertEqual(app.server_entry.get(), "http://127.0.0.1:8765")

    def test_optional_token_hidden_until_enabled(self):
        app = self.open_app()
        self.assertFalse(app.token_entry.grid_info())
        app.use_auth.set(True)
        app._toggle_auth()
        self.assertTrue(app.token_entry.grid_info())

    def test_lock_without_password_waits_for_ack_and_saves_marker(self):
        app = self.open_app()
        app.audio_player = Mock(is_playing=True)
        app.audio_player.start_playback.return_value = True
        app.audio_player.is_callback_healthy.return_value = True
        app.audio_player.get_status.return_value = dict(volume=.5, is_playing=True, callback_age=0)
        app.tamper_monitor.start_monitoring = Mock(return_value=(True, "OK"))
        app.tamper_monitor.get_diagnostic_info = Mock(return_value={})
        communicator = Mock()
        communicator.get_connection_status.return_value = (True, "Connected")
        with patch.object(client, "ServerCommunicator", return_value=communicator):
            app._lock_system()
            self.pump(lambda: app.is_locked)
        self.assertTrue(self.store.load()["lock_active"])
        communicator.update_state.assert_called()
        communicator.first_response.wait.assert_called_once_with(5)

    def test_rejected_lock_returns_to_setup(self):
        app = self.open_app()
        app.audio_player = Mock(is_playing=True)
        app.audio_player.start_playback.return_value = True
        app.audio_player.is_callback_healthy.return_value = True
        app.tamper_monitor.start_monitoring = Mock(return_value=(True, "OK"))
        communicator = Mock()
        communicator.get_connection_status.return_value = (False, "HTTP 403")
        with patch.object(client, "ServerCommunicator", return_value=communicator):
            app._lock_system()
            self.pump(lambda: app._job_thread is None)
        self.assertFalse(app.is_locked)
        self.assertFalse(self.store.load()["lock_active"])
        app.audio_player.stop_playback.assert_called_once()

    def test_interrupted_lock_requires_builtin_password(self):
        self.store.save(dict(lock_active=True, device_name="USB Headset", tamper_flags={"device_muted": True}))
        communicator = Mock()
        communicator.get_connection_status.return_value = (True, "Connected")
        with patch.object(client, "ServerCommunicator", return_value=communicator):
            app = self.open_app()
            self.pump(lambda: app.is_locked)
        self.assertTrue(app.tamper_monitor.get_tamper_status()[1]["client_restarted"])
        self.assertTrue(app.tamper_monitor.get_tamper_status()[1]["device_muted"])
        with patch.object(client.simpledialog, "askstring", return_value="wrong"):
            app._unlock_system()
        self.assertTrue(app.is_locked)
        with patch.object(client.simpledialog, "askstring", return_value=client.ADMIN_PASSWORD):
            app._unlock_system()
            self.pump(lambda: not app.is_locked)
        self.assertFalse(self.store.load()["lock_active"])
        self.assertEqual(self.store.load()["tamper_flags"], {})

    def test_window_processes_events_while_lock_worker_waits(self):
        app = self.open_app()
        release = threading.Event()
        with patch.object(app, "_prepare_lock", side_effect=lambda: release.wait(2)):
            app._lock_system()
            ticked = []
            app.after(0, lambda: ticked.append(True))
            self.pump(lambda: bool(ticked))
            self.assertFalse(app.is_locked)
            release.set()
            self.pump(lambda: app._job_thread is None)

    def test_timed_out_lock_cleans_up_before_retry(self):
        app = self.open_app()
        release = threading.Event()
        app._finish_unlock = Mock()
        with patch.object(app, "_prepare_lock", side_effect=lambda: release.wait(2)):
            app._lock_system()
            app._job_started = time.monotonic() - client.LOCK_TIMEOUT_SECONDS - 1
            self.pump(lambda: app._job_timed_out)
            self.assertEqual(str(app.lock_button["state"]), "disabled")
            release.set()
            self.pump(lambda: app._job_thread is None)
        self.assertFalse(app.is_locked)
        self.assertFalse(self.store.load()["lock_active"])
        app._finish_unlock.assert_called_once()

    def test_lock_cancels_old_test_stop_timer(self):
        app = self.open_app()
        timer = app.after(3000, lambda: None)
        app._test_stop_timer = timer
        with patch.object(app, "_begin_job"):
            app._lock_system()
        self.assertIsNone(app._test_stop_timer)
        self.assertNotIn(timer, app.tk.call("after", "info"))

    def test_connection_check_does_not_start_playback_or_lock(self):
        app = self.open_app()
        app.audio_player.start_playback = Mock()
        communicator = Mock()
        communicator.get_connection_status.return_value = (True, "Connected")
        with patch.object(client, "ServerCommunicator", return_value=communicator):
            app._check_server_connection()
            self.pump(lambda: app._job_thread is None)
        app.audio_player.start_playback.assert_not_called()
        self.assertFalse(app.is_locked)
        self.assertIn("Server accepted a heartbeat", app.setup_status["text"])
        communicator.stop.assert_called_once()


class MonitorThreadTests(unittest.TestCase):
    def test_com_initialization_reads_and_release_share_thread(self):
        import comtypes
        calls = []
        monitor = client.TamperMonitor(Mock())
        class FakeMonitor:
            def initialize(self, *args):
                calls.append(("initialize", threading.get_ident()))
                return True, "OK"
        def loop():
            calls.append(("read", threading.get_ident()))
        with patch.object(client, "WindowsAudioMonitor", FakeMonitor), \
             patch.object(comtypes, "CoInitialize", side_effect=lambda: calls.append(("com_init", threading.get_ident()))), \
             patch.object(comtypes, "CoUninitialize", side_effect=lambda: calls.append(("com_release", threading.get_ident()))), \
             patch.object(monitor, "_monitor_loop", side_effect=loop):
            self.assertTrue(monitor.start_monitoring(1, "USB", Mock())[0])
            monitor.thread.join(1)
        self.assertEqual([name for name, _ in calls], ["com_init", "initialize", "read", "com_release"])
        self.assertEqual(len(set(identity for _, identity in calls)), 1)
        self.assertNotEqual(calls[0][1], threading.get_ident())


if __name__ == "__main__":
    unittest.main()
