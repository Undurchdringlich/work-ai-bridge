"""
Unit and integration tests for Work-AI Bridge.
Tests interface detection, argument parsing, upstream connectivity probe,
and end-to-end HTTP/HTTPS CONNECT proxy functionality using only the standard library.
"""

import http.server
import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

# Ensure scripts/ directory is in sys.path
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import bridge_proxy  # noqa: E402


class TestNetworkDetection(unittest.TestCase):
    """Tests for network interface heuristic detection."""

    def test_detect_openvpn_prefers_10_subnet(self):
        interfaces = [
            ("192.168.1.50", "Wi-Fi"),
            ("10.50.100.25", "OpenVPN TAP-Windows Adapter V9"),
            ("172.29.0.1", "vEthernet (WSL)"),
        ]
        ip, name = bridge_proxy.detect_openvpn_ip(interfaces)
        self.assertEqual(ip, "10.50.100.25")
        self.assertIn("OpenVPN", name)

    def test_detect_openvpn_prefers_172_corporate_subnet(self):
        interfaces = [
            ("192.168.0.10", "Ethernet"),
            ("172.20.10.5", "Corporate VPN"),
        ]
        ip, name = bridge_proxy.detect_openvpn_ip(interfaces)
        self.assertEqual(ip, "172.20.10.5")

    def test_detect_openvpn_ignores_amnezia_and_radmin(self):
        interfaces = [
            ("10.8.0.2", "AmneziaVPN Tunnel"),
            ("10.100.0.5", "Radmin VPN Network"),
            ("192.168.1.15", "Ethernet 2"),
        ]
        ip, name = bridge_proxy.detect_openvpn_ip(interfaces)
        # Amnezia and Radmin must be skipped
        self.assertEqual(ip, "192.168.1.15")

    def test_detect_openvpn_matches_vpn_keywords(self):
        interfaces = [
            ("192.168.100.2", "Подключение по локальной сети"),
            ("192.168.50.2", "TAP-Windows Corporate Adapter"),
        ]
        ip, name = bridge_proxy.detect_openvpn_ip(interfaces)
        self.assertEqual(ip, "192.168.50.2")

    def test_detect_openvpn_fallback_empty(self):
        ip, name = bridge_proxy.detect_openvpn_ip([])
        self.assertEqual(ip, "127.0.0.1")

    def test_decode_bytes_cp866(self):
        # Simulates Russian Windows PowerShell output in CP866
        raw = "Подключение по локальной сети 2".encode("cp866")
        decoded = bridge_proxy.decode_bytes(raw)
        self.assertEqual(decoded, "Подключение по локальной сети 2")

    def test_decode_bytes_utf8(self):
        raw = "Беспроводная сеть".encode("utf-8")
        decoded = bridge_proxy.decode_bytes(raw)
        self.assertEqual(decoded, "Беспроводная сеть")


class TestUpstreamProbe(unittest.TestCase):
    """Tests for upstream Google Gemini AI probe."""

    @patch("urllib.request.urlopen")
    def test_probe_200_ok(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        ok, msg = bridge_proxy.check_upstream_ai("https://example.com")
        self.assertTrue(ok)
        self.assertIn("200", msg)

    @patch("urllib.request.urlopen")
    def test_probe_google_ai_root_404_considered_success(self, mock_urlopen):
        # Google Generative Language API root returns 404 by design, confirming connection to Google
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        ok, msg = bridge_proxy.check_upstream_ai()
        self.assertTrue(ok)
        self.assertIn("404", msg)

    @patch("urllib.request.urlopen")
    def test_probe_network_failure(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        ok, msg = bridge_proxy.check_upstream_ai()
        self.assertFalse(ok)
        self.assertIn("Connection refused", msg)


class TestCLIArgs(unittest.TestCase):
    """Tests for command-line arguments parsing."""

    def test_default_args(self):
        args = bridge_proxy.parse_args([])
        self.assertEqual(args.port, 8080)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertIsNone(args.ip)
        self.assertEqual(args.corp_domains, "")
        self.assertFalse(args.skip_probe)

    def test_custom_args(self):
        args = bridge_proxy.parse_args([
            "--port", "9090",
            "--host", "127.0.0.1",
            "--ip", "10.0.0.99",
            "--corp-domains", "*.mycorp.internal,*.mycorp.local",
            "--skip-probe",
            "--stream-timeout", "900",
        ])
        self.assertEqual(args.port, 9090)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.ip, "10.0.0.99")
        self.assertEqual(args.corp_domains, "*.mycorp.internal,*.mycorp.local")
        self.assertTrue(args.skip_probe)
        self.assertEqual(args.stream_timeout, 900)


class TestSocketOptions(unittest.TestCase):
    """Tests for socket keepalive configuration."""

    def test_enable_keepalive(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bridge_proxy.enable_keepalive(s)
            val = s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            self.assertNotEqual(val, 0)
        finally:
            s.close()


class DummyEchoHandler(http.server.BaseHTTPRequestHandler):
    """Simple HTTP handler returning 200 OK for testing."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"HELLO_FROM_DUMMY_SERVER")

    def log_message(self, format, *args):
        pass  # Suppress server log noise during tests


class TestProxyServerIntegration(unittest.TestCase):
    """Integration test: spins up a dummy HTTP server and proxy on ephemeral ports."""

    @classmethod
    def setUpClass(cls):
        # 1. Start a local HTTP target server
        cls.target_server = http.server.HTTPServer(("127.0.0.1", 0), DummyEchoHandler)
        cls.target_port = cls.target_server.server_port
        cls.target_thread = threading.Thread(target=cls.target_server.serve_forever, daemon=True)
        cls.target_thread.start()

        # 2. Start ThreadedProxyServer on an ephemeral port
        cls.proxy_server = bridge_proxy.ThreadedProxyServer(
            host="127.0.0.1",
            port=0,
            log_file="",
            stream_timeout=10,
        )
        # Manually bind to port 0 to get an OS-assigned free port
        cls.proxy_server.server_socket.bind(("127.0.0.1", 0))
        cls.proxy_port = cls.proxy_server.server_socket.getsockname()[1]
        cls.proxy_server.server_socket.listen(128)
        cls.proxy_server.running = True

        cls.proxy_thread = threading.Thread(
            target=cls._run_proxy_loop,
            args=(cls.proxy_server,),
            daemon=True,
        )
        cls.proxy_thread.start()
        time.sleep(0.1)

    @classmethod
    def _run_proxy_loop(cls, server):
        while server.running:
            try:
                sock, addr = server.server_socket.accept()
                t = threading.Thread(target=server.handle_client, args=(sock, addr), daemon=True)
                t.start()
            except Exception:
                break

    @classmethod
    def tearDownClass(cls):
        cls.proxy_server.stop()
        cls.target_server.shutdown()
        cls.target_server.server_close()

    def test_http_forwarding(self):
        """Tests standard HTTP proxy forwarding (GET http://host:port/path)."""
        client = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            req = (
                f"GET http://127.0.0.1:{self.target_port}/test HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.target_port}\r\n"
                f"Connection: close\r\n\r\n"
            )
            client.sendall(req.encode("utf-8"))

            chunks = []
            while True:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
            resp = b"".join(chunks).decode("utf-8", errors="replace")
            self.assertIn("200 OK", resp)
            self.assertIn("HELLO_FROM_DUMMY_SERVER", resp)
        finally:
            client.close()

    def test_https_connect_tunneling(self):
        """Tests HTTPS CONNECT tunneling (HTTP/1.1 200 Connection Established)."""
        client = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            connect_req = f"CONNECT 127.0.0.1:{self.target_port} HTTP/1.1\r\nHost: 127.0.0.1:{self.target_port}\r\n\r\n"
            client.sendall(connect_req.encode("utf-8"))

            header = client.recv(1024).decode("utf-8", errors="replace")
            self.assertIn("200 Connection Established", header)

            # Now send raw data through the established tunnel
            http_req = b"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            client.sendall(http_req)

            chunks = []
            while True:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
            resp = b"".join(chunks).decode("utf-8", errors="replace")
            self.assertIn("200 OK", resp)
            self.assertIn("HELLO_FROM_DUMMY_SERVER", resp)
        finally:
            client.close()

    def test_connect_unreachable_target_returns_502(self):
        """Tests that attempting to tunnel to an unreachable port returns 502 Bad Gateway."""
        client = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            # Port 1 is typically unused / closed
            connect_req = "CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n"
            client.sendall(connect_req.encode("utf-8"))

            resp = client.recv(1024).decode("utf-8", errors="replace")
            self.assertIn("502 Bad Gateway", resp)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
