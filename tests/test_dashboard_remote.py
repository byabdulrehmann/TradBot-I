import unittest
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer


class DashboardRemoteAccessTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()
        self.server._ip = "192.168.1.20"

    def test_phone_url_uses_http_even_when_certificate_is_available(self):
        with patch.object(DashboardServer, "_ssl_enabled", return_value=True):
            self.assertEqual(self.server.get_url(), "http://192.168.1.20:8000")

    def test_phone_url_falls_back_to_http_without_certificate(self):
        with patch.object(DashboardServer, "_ssl_enabled", return_value=False):
            self.assertEqual(self.server.get_url(), "http://192.168.1.20:8000")

    def test_qr_link_pairs_phone_and_creates_device_token(self):
        key = self.server.new_key()

        with TestClient(self.server.app) as client:
            response = client.get("/auto-login", params={"key": key})
            screen = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("sessionStorage.setItem('jarvis_token'", response.text)
        self.assertIn(
            "try { localStorage.setItem('jarvis_device_token'",
            response.text,
        )
        self.assertIn("location.replace('/')", response.text)
        self.assertEqual(screen.status_code, 200)
        self.assertIn("<title>JARVIS Dashboard</title>", screen.text)
        self.assertIn('id="feed"', screen.text)
        self.assertNotIn(key, self.server._pending_keys)
        self.assertEqual(len(self.server._device_sessions), 1)

    def test_phone_audio_uses_16khz_pcm_metadata(self):
        token = "test-phone-token"
        self.server._tokens.add(token)
        audio = b"\x01\x00" * 16

        with TestClient(self.server.app) as client:
            with client.websocket_connect(f"/ws/phone-audio?token={token}") as ws:
                ws.send_bytes(audio)
                deadline = time.monotonic() + 1
                while self.server._phone_audio_queue.empty() and time.monotonic() < deadline:
                    time.sleep(0.01)
                chunk = self.server._phone_audio_queue.get_nowait()

        self.assertEqual(chunk["data"], audio)
        self.assertEqual(chunk["mime_type"], "audio/pcm;rate=16000")


if __name__ == "__main__":
    unittest.main()
