"""
Work-AI Bridge Proxy
--------------------
A lightweight, zero-dependency HTTP/HTTPS CONNECT proxy server designed
to bridge AI developer tools (Antigravity IDE, Cursor, Claude Code, Copilot, etc.)
from a restricted corporate workstation (via RDP) to a home PC with unrestricted AI access.

Author: Undurchdringlich
License: MIT
"""

import argparse
import datetime
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

__version__ = "1.0.0"

# Fix UTF-8 output on Windows terminal
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_PORT = 8080
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PROBE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_LOG_FILE = "bridge_traffic.log"
DEFAULT_STREAM_TIMEOUT = 1800  # 30 minutes for long LLM streaming (Extended Thinking / SSE / gRPC)


def decode_bytes(data: bytes) -> str:
    """
    Robustly decodes byte output from subprocesses.
    Tries strict UTF-8 first, then falls back to Windows Russian OEM (CP866),
    Windows ANSI (CP1251), and MBCS before falling back to replace.
    """
    if not data:
        return ""

    # 1. Try strict UTF-8
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2. Try Windows codepages (PowerShell on Russian Windows defaults to CP866)
    for enc in ("cp866", "cp1251", "mbcs"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass

    # 3. Final fallback
    return data.decode("utf-8", errors="replace")


def get_network_interfaces() -> List[Tuple[str, str]]:
    """Detect available IPv4 network interfaces on the local machine."""
    ips: List[Tuple[str, str]] = []

    # Windows: PowerShell query for accurate interface names and addresses
    if sys.platform == "win32":
        try:
            cmd = (
                '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
                '[Console]::InputEncoding = [System.Text.Encoding]::UTF8; '
                'Get-NetIPAddress -AddressFamily IPv4 | '
                'Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | '
                'Select-Object IPAddress, InterfaceAlias | ConvertTo-Json'
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                timeout=5,
            )
            raw_output = decode_bytes(res.stdout).strip()
            if raw_output:
                data = json.loads(raw_output)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    ip = item.get("IPAddress", "")
                    alias = item.get("InterfaceAlias", "")
                    if ip:
                        ips.append((ip, alias))
        except Exception:
            pass

    # Fallback or Unix/macOS: standard socket discovery
    if not ips:
        try:
            hostname = socket.gethostname()
            for addr in socket.getaddrinfo(hostname, None):
                ip = addr[4][0]
                if ":" not in ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                    if (ip, "Local Interface") not in ips:
                        ips.append((ip, "Local Interface"))
        except Exception:
            pass

    return ips


def detect_openvpn_ip(interfaces: List[Tuple[str, str]]) -> Tuple[str, str]:
    """
    Finds the IP address of the corporate VPN/OpenVPN tunnel.
    Filters out local egress VPNs like Amnezia and virtual adapters like Radmin.
    """
    excluded_keywords = ("amnezia", "radmin", "virtual", "docker", "vethernet", "loopback")

    # 1. Search for typical corporate private subnets (10.x.x.x or 172.16-31.x.x)
    for ip, name in interfaces:
        lower_name = name.lower()
        if any(kw in lower_name for kw in excluded_keywords):
            continue
        try:
            parts = ip.split(".")
            if len(parts) == 4:
                first = int(parts[0])
                second = int(parts[1])
                if first == 10:
                    return ip, name
                if first == 172 and 16 <= second <= 31:
                    return ip, name
        except (ValueError, IndexError):
            continue

    # 2. Search for interface names containing VPN keywords
    vpn_keywords = ("vpn", "tap", "tun", "openvpn", "сеть", "corporate")
    for ip, name in interfaces:
        lower_name = name.lower()
        if any(kw in lower_name for kw in excluded_keywords):
            continue
        if any(kw in lower_name for kw in vpn_keywords):
            return ip, name

    # 3. Any non-excluded interface
    for ip, name in interfaces:
        lower_name = name.lower()
        if not any(kw in lower_name for kw in excluded_keywords):
            return ip, name

    if interfaces:
        return interfaces[0][0], interfaces[0][1]

    return "127.0.0.1", "Localhost"


def check_upstream_ai(probe_url: str = DEFAULT_PROBE_URL) -> Tuple[bool, str]:
    """
    Checks if upstream AI endpoints (e.g. Google Gemini API) are reachable from this machine.
    HTTP 404 on the API root is expected and confirms successful network routing to Google.
    """
    try:
        req = urllib.request.Request(
            probe_url,
            headers={"User-Agent": "Work-AI-Bridge-Probe/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # HTTP 404 or 403 on root endpoint confirms upstream connection is established
        if e.code in (404, 403):
            return True, f"Connection OK (HTTP {e.code})"
        return True, f"Reachable (HTTP {e.code})"
    except Exception as e:
        return False, str(e)


def enable_keepalive(sock: socket.socket) -> None:
    """
    Enables TCP KeepAlive to prevent intermediate NAT/firewalls from dropping idle
    connections during long LLM extended thinking or streaming generations.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        # Windows-specific: 10s idle, 3s retry interval
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
        else:
            # Linux / macOS
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    except Exception:
        pass


def pipe_sockets(src: socket.socket, dst: socket.socket, stop_event: threading.Event) -> None:
    """Bidirectional full-duplex socket pipe without data loss under high traffic."""
    try:
        while not stop_event.is_set():
            try:
                data = src.recv(65536)
            except (socket.timeout, OSError):
                break
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        stop_event.set()
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def free_port(port: int) -> None:
    """Automatically frees the port on Windows if occupied by an orphaned process."""
    if sys.platform != "win32":
        return

    try:
        cmd = f"netstat -ano | findstr :{port} | findstr LISTENING"
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
        current_pid = os.getpid()
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5:
                try:
                    pid = int(parts[-1])
                    if pid != current_pid and pid != 0:
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
                except ValueError:
                    continue
    except Exception:
        pass


class ThreadedProxyServer:
    """Multi-threaded HTTP/HTTPS CONNECT proxy server with zero external dependencies."""

    def __init__(
        self,
        host: str,
        port: int,
        log_file: str = DEFAULT_LOG_FILE,
        stream_timeout: int = DEFAULT_STREAM_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.log_file = log_file
        self.stream_timeout = stream_timeout
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        else:
            try:
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass

        self.running = False

    def log(self, client_ip: str, method: str, target: str, status: str, details: str = "") -> None:
        """Formats and logs proxy traffic to console and log file."""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] {client_ip:<15} | {method:<7} {target:<45} | {status} {details}".rstrip()
        print(line, flush=True)
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def handle_client(self, client_socket: socket.socket, client_addr: Tuple[str, int]) -> None:
        """Processes an incoming client connection (CONNECT or standard HTTP)."""
        client_ip = client_addr[0]
        remote_socket: Optional[socket.socket] = None

        try:
            client_socket.settimeout(15)
            req_data = client_socket.recv(8192)
            if not req_data:
                return

            header_end = req_data.find(b"\r\n")
            if header_end == -1:
                return

            first_line = req_data[:header_end].decode("utf-8", errors="ignore")
            parts = first_line.split()
            if len(parts) < 2:
                return

            method, url = parts[0].upper(), parts[1]

            if method == "CONNECT":
                # HTTPS Tunneling
                if ":" in url:
                    target_host, target_port_str = url.split(":", 1)
                    target_port = int(target_port_str)
                else:
                    target_host = url
                    target_port = 443

                try:
                    remote_socket = socket.create_connection((target_host, target_port), timeout=15)
                    enable_keepalive(client_socket)
                    enable_keepalive(remote_socket)
                    client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self.log(client_ip, "CONNECT", f"{target_host}:{target_port}", "200 OK")
                except Exception as e:
                    self.log(client_ip, "CONNECT", f"{target_host}:{target_port}", "ERROR", str(e))
                    try:
                        client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    except Exception:
                        pass
                    return

                # Long timeout for SSE / streaming LLM responses
                client_socket.settimeout(self.stream_timeout)
                remote_socket.settimeout(self.stream_timeout)

                stop_event = threading.Event()
                t1 = threading.Thread(
                    target=pipe_sockets,
                    args=(client_socket, remote_socket, stop_event),
                    daemon=True,
                )
                t2 = threading.Thread(
                    target=pipe_sockets,
                    args=(remote_socket, client_socket, stop_event),
                    daemon=True,
                )
                t1.start()
                t2.start()
                t1.join()
                t2.join()

            else:
                # Standard HTTP (GET, POST, etc.)
                match = re.match(r"^https?://([^/]+)(.*)$", url)
                if match:
                    host_part, path = match.group(1), match.group(2)
                else:
                    host_part = url
                    path = "/"

                if ":" in host_part:
                    target_host, target_port_str = host_part.split(":", 1)
                    target_port = int(target_port_str)
                else:
                    target_host = host_part
                    target_port = 80

                try:
                    remote_socket = socket.create_connection((target_host, target_port), timeout=15)
                    enable_keepalive(client_socket)
                    enable_keepalive(remote_socket)
                    remote_socket.sendall(req_data)
                    self.log(client_ip, method, f"{target_host}:{target_port}{path[:20]}", "FORWARD")
                except Exception as e:
                    self.log(client_ip, method, f"{target_host}:{target_port}", "ERROR", str(e))
                    try:
                        client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    except Exception:
                        pass
                    return

                client_socket.settimeout(self.stream_timeout)
                remote_socket.settimeout(self.stream_timeout)

                stop_event = threading.Event()
                t1 = threading.Thread(
                    target=pipe_sockets,
                    args=(client_socket, remote_socket, stop_event),
                    daemon=True,
                )
                t2 = threading.Thread(
                    target=pipe_sockets,
                    args=(remote_socket, client_socket, stop_event),
                    daemon=True,
                )
                t1.start()
                t2.start()
                t1.join()
                t2.join()

        except Exception:
            pass
        finally:
            try:
                client_socket.close()
            except Exception:
                pass
            if remote_socket:
                try:
                    remote_socket.close()
                except Exception:
                    pass

    def start(self) -> None:
        """Binds to host:port and begins accepting incoming connections."""
        try:
            self.server_socket.bind((self.host, self.port))
        except OSError as e:
            # Address already in use
            if getattr(e, "winerror", 0) == 10048 or getattr(e, "errno", 0) in (10048, 98):
                print(f"[*] Port {self.port} is busy. Attempting to free it...", flush=True)
                free_port(self.port)
                import time
                time.sleep(1)
                self.server_socket.bind((self.host, self.port))
            else:
                raise

        self.server_socket.listen(128)
        self.running = True

        while self.running:
            try:
                client_sock, client_addr = self.server_socket.accept()
                t = threading.Thread(
                    target=self.handle_client,
                    args=(client_sock, client_addr),
                    daemon=True,
                )
                t.start()
            except KeyboardInterrupt:
                break
            except Exception:
                if not self.running:
                    break

        self.stop()

    def stop(self) -> None:
        """Gracefully terminates the proxy server."""
        self.running = False
        try:
            self.server_socket.close()
        except Exception:
            pass


def parse_args(args_list: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Work-AI Bridge: Lightweight HTTP/HTTPS proxy bridge for AI tools from corporate RDP environments.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port to listen on.",
    )
    parser.add_argument(
        "-b", "--host",
        type=str,
        default=DEFAULT_HOST,
        help="Host address to bind to.",
    )
    parser.add_argument(
        "--ip",
        type=str,
        default=None,
        help="Explicit VPN IP to show in instructions (overrides automatic detection).",
    )
    parser.add_argument(
        "--corp-domains",
        type=str,
        default="",
        help="Comma-separated internal domains to exclude via NO_PROXY (e.g. '*.company.com,*.corp.local').",
    )
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Skip pre-flight upstream AI connectivity probe.",
    )
    parser.add_argument(
        "--probe-url",
        type=str,
        default=DEFAULT_PROBE_URL,
        help="Target URL for upstream connectivity probe.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=DEFAULT_LOG_FILE,
        help="File to append traffic logs to (pass empty string to disable file logging).",
    )
    parser.add_argument(
        "--stream-timeout",
        type=int,
        default=DEFAULT_STREAM_TIMEOUT,
        help="Timeout (in seconds) for long LLM streaming sessions.",
    )
    return parser.parse_args(args_list)


def main() -> None:
    args = parse_args()

    print("=" * 76, flush=True)
    print("      >>> WORK-AI BRIDGE PROXY (FOR ANTIGRAVITY & AI TOOLS) <<<      ", flush=True)
    print("=" * 76, flush=True)

    # 1. Interface Discovery
    interfaces = get_network_interfaces()
    if args.ip:
        detected_ip = args.ip
        iface_name = "User specified"
    else:
        detected_ip, iface_name = detect_openvpn_ip(interfaces)

    print("\n[*] Обнаружение сетевых интерфейсов:", flush=True)
    if interfaces:
        for ip, name in interfaces:
            tag = " <--- (Определен как туннель OpenVPN)" if ip == detected_ip else ""
            print(f"   * [{name}] : {ip}{tag}", flush=True)
    else:
        print(f"   * [Локальный адрес] : {detected_ip}", flush=True)

    # 2. Connectivity Probe
    if not args.skip_probe:
        print(f"\n[*] Проверка связи с upstream AI ({args.probe_url})...", flush=True)
        ai_ok, ai_msg = check_upstream_ai(args.probe_url)
        if ai_ok:
            print(f"   [УСПЕШНО] Upstream доступен! ({ai_msg})", flush=True)
        else:
            print(f"   [!] ВНИМАНИЕ: Не удалось связаться с AI сервисом: {ai_msg}", flush=True)
            print("   Убедитесь, что AmneziaVPN / шлюз в интернет запущен и подключен на домашнем ПК.", flush=True)

    # 3. Instruction Setup
    port = args.port
    listen_ip = args.host

    no_proxy_domains = "localhost,127.0.0.1"
    if args.corp_domains:
        clean_domains = ",".join(d.strip() for d in args.corp_domains.split(",") if d.strip())
        no_proxy_domains += f",{clean_domains}"
    else:
        no_proxy_domains += ",*.corp.yourcompany.com"

    print(f"\n[+] Прокси запущен на {listen_ip}:{port} (Доступен для рабочей машины: {detected_ip}:{port})", flush=True)
    print("-" * 76, flush=True)
    print(">>> ИНСТРУКЦИЯ ДЛЯ РАБОЧЕЙ МАШИНЫ (выполнять внутри RDP-сессии):", flush=True)
    print("-" * 76, flush=True)
    print("1. Проверка связи в PowerShell на рабочей машине:", flush=True)
    print(f"   curl.exe -I -x http://{detected_ip}:{port} https://generativelanguage.googleapis.com\n", flush=True)
    print("2. Установка переменных окружения для Antigravity / AI инструментов (PowerShell):", flush=True)
    print(f'   [System.Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://{detected_ip}:{port}", "User")', flush=True)
    print(f'   [System.Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://{detected_ip}:{port}", "User")', flush=True)
    print(f'   [System.Environment]::SetEnvironmentVariable("NO_PROXY", "{no_proxy_domains}", "User")\n', flush=True)
    if not args.corp_domains:
        print("   (Подсказка: Замените *.corp.yourcompany.com на реальные домены вашей компании)", flush=True)
        print("   (Либо запустите скрипт с флагом: python scripts/bridge_proxy.py --corp-domains \"*.company.com,*.corp\")\n", flush=True)
    print("3. Настройка интерфейса Antigravity IDE (Settings -> Ctrl+, -> поиск 'proxy'):", flush=True)
    print(f"   Http: Proxy          -->  http://{detected_ip}:{port}", flush=True)
    print("   Http: Proxy Support  -->  override", flush=True)
    print("-" * 76, flush=True)
    print(">>> ЖУРНАЛ ЗАПРОСОВ (Для остановки нажмите Ctrl+C):", flush=True)
    print(f"{'ВРЕМЯ':<10} {'IP КЛИЕНТА':<15} | {'МЕТОД':<7} {'ЦЕЛЕВОЙ РЕСУРС':<45} | СТАТУС", flush=True)
    print("-" * 76, flush=True)

    server = ThreadedProxyServer(
        host=listen_ip,
        port=port,
        log_file=args.log_file,
        stream_timeout=args.stream_timeout,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n\nОстановка прокси-моста...", flush=True)
        server.stop()
        print("Прокси-мост успешно остановлен.", flush=True)


if __name__ == "__main__":
    main()
