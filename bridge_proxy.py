"""
Work-AI Bridge Proxy
Легковесный HTTP/HTTPS CONNECT прокси-сервер с нулевыми внешними зависимостями
для проброса трафика Antigravity и других ИИ-инструментов с рабочей машины
на домашний ПК через Amnezia/VPN.
"""

import sys
import socket
import select
import threading
import datetime
import urllib.request
import subprocess
import json
import re

# Настройка корректного вывода UTF-8 (кириллицы) в консоли Windows
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

def get_network_interfaces():
    """Определяет доступные сетевые интерфейсы IPv4 на компьютере."""
    ips = []
    try:
        cmd = 'Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object IPAddress, InterfaceAlias | ConvertTo-Json'
        res = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=5)
        raw_output = res.stdout.decode("utf-8", errors="replace")
        data = json.loads(raw_output)
        if isinstance(data, dict):
            data = [data]
        for item in data:
            ip = item.get("IPAddress", "")
            alias = item.get("InterfaceAlias", "")
            if ip:
                ips.append((ip, alias))
    except Exception:
        # Резервный метод через стандартные сокеты
        try:
            hostname = socket.gethostname()
            for addr in socket.getaddrinfo(hostname, None):
                ip = addr[4][0]
                if ":" not in ip and not ip.startswith("127."):
                    ips.append((ip, "Локальный интерфейс"))
        except Exception:
            pass
    return ips

def detect_openvpn_ip(interfaces):
    """Находит IP-адрес корпоративного OpenVPN туннеля (исключая Amnezia и Radmin)."""
    # 1. Поиск корпоративных подсетей (10.x или 172.x), исключая Amnezia и Radmin
    for ip, name in interfaces:
        lower_name = name.lower()
        if "amnezia" in lower_name or "radmin" in lower_name:
            continue
        if ip.startswith("10.") or ip.startswith("172."):
            return ip, name

    # 2. Поиск сетевых адаптеров с ключевыми словами
    for ip, name in interfaces:
        lower_name = name.lower()
        if "amnezia" in lower_name or "radmin" in lower_name:
            continue
        if "сеть" in lower_name or "tap" in lower_name or "openvpn" in lower_name:
            return ip, name

    # 3. Любой не-Amnezia интерфейс
    for ip, name in interfaces:
        lower_name = name.lower()
        if "amnezia" not in lower_name and "radmin" not in lower_name:
            return ip, name

    if interfaces:
        return interfaces[0][0], interfaces[0][1]
    return "0.0.0.0", "Все интерфейсы"

def check_upstream_ai():
    """Проверяет доступность серверов Google Gemini API через Amnezia/домашний интернет."""
    test_url = "https://generativelanguage.googleapis.com"
    try:
        req = urllib.request.Request(test_url, headers={"User-Agent": "Antigravity-Bridge-Probe"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"Код {resp.status}"
    except urllib.error.HTTPError as e:
        # Код 404 для корня API Google Generative Language является нормой и подтверждает связь
        return True, f"Связь есть (HTTP {e.code})"
    except Exception as e:
        return False, str(e)

LOG_FILE = "bridge_traffic.log"
STREAM_IDLE_TIMEOUT = 1800  # 30 минут таймаута для длительных потоковых (SSE/gRPC) ответов ИИ

def enable_keepalive(sock):
    """Включает TCP KeepAlive для предотвращения обрыва сессии фаерволами во время размышлений ИИ."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            # Windows: опрос каждые 3 секунды после 10 секунд простоя
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
    except Exception:
        pass

def pipe_sockets(src, dst, stop_event):
    """Надежная двунаправленная пересылка данных без потерь при больших нагрузках."""
    try:
        while not stop_event.is_set():
            try:
                data = src.recv(65536)
            except socket.timeout:
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

def free_port(port):
    """Автоматически освобождает порт, если он остался занят зависшим процессом."""
    try:
        import os
        cmd = f"netstat -ano | findstr :{port} | findstr LISTENING"
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
        current_pid = os.getpid()
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5:
                pid = int(parts[-1])
                if pid != current_pid and pid != 0:
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
    except Exception:
        pass

class ThreadedProxyServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        self.running = False

    def log(self, client_ip, method, target, status, details=""):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] {client_ip:<15} | {method:<7} {target:<45} | {status} {details}"
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def handle_client(self, client_socket, client_addr):
        client_ip = client_addr[0]
        remote_socket = None
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
                # Туннелирование HTTPS
                if ":" in url:
                    target_host, target_port = url.split(":", 1)
                    target_port = int(target_port)
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
                    self.log(client_ip, "CONNECT", f"{target_host}:{target_port}", "ОШИБКА", str(e))
                    client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return

                # Надежная двунаправленная пересылка данных через потоки
                client_socket.settimeout(STREAM_IDLE_TIMEOUT)
                remote_socket.settimeout(STREAM_IDLE_TIMEOUT)

                stop_event = threading.Event()
                t1 = threading.Thread(target=pipe_sockets, args=(client_socket, remote_socket, stop_event), daemon=True)
                t2 = threading.Thread(target=pipe_sockets, args=(remote_socket, client_socket, stop_event), daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()
            else:
                # Обычный HTTP (GET, POST и т.д.)
                match = re.match(r"^https?://([^/]+)(.*)$", url)
                if match:
                    host_part, path = match.group(1), match.group(2)
                else:
                    host_part = url
                    path = "/"

                if ":" in host_part:
                    target_host, target_port = host_part.split(":", 1)
                    target_port = int(target_port)
                else:
                    target_host = host_part
                    target_port = 80

                try:
                    remote_socket = socket.create_connection((target_host, target_port), timeout=15)
                    enable_keepalive(client_socket)
                    enable_keepalive(remote_socket)
                    remote_socket.sendall(req_data)
                    self.log(client_ip, method, f"{target_host}:{target_port}{path[:20]}", "ПЕРЕСЫЛКА")
                except Exception as e:
                    self.log(client_ip, method, f"{target_host}:{target_port}", "ОШИБКА", str(e))
                    client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return

                client_socket.settimeout(STREAM_IDLE_TIMEOUT)
                remote_socket.settimeout(STREAM_IDLE_TIMEOUT)

                stop_event = threading.Event()
                t1 = threading.Thread(target=pipe_sockets, args=(client_socket, remote_socket, stop_event), daemon=True)
                t2 = threading.Thread(target=pipe_sockets, args=(remote_socket, client_socket, stop_event), daemon=True)
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

    def start(self):
        try:
            self.server_socket.bind((self.host, self.port))
        except OSError as e:
            if getattr(e, "winerror", 0) == 10048 or getattr(e, "errno", 0) in (10048, 98):
                print(f"[*] Порт {self.port} был занят старым процессом. Освобождаю...", flush=True)
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
                t = threading.Thread(target=self.handle_client, args=(client_sock, client_addr), daemon=True)
                t.start()
            except KeyboardInterrupt:
                break
            except Exception:
                if not self.running:
                    break
        self.stop()

    def stop(self):
        self.running = False
        try:
            self.server_socket.close()
        except Exception:
            pass

def main():
    print("=" * 76, flush=True)
    print("      >>> ПРОКСИ-МОСТ ДЛЯ ANTIGRAVITY / ИИ (ДОМАШНИЙ ПК) <<<        ", flush=True)
    print("=" * 76, flush=True)

    # 1. Поиск интерфейсов
    interfaces = get_network_interfaces()
    detected_ip, iface_name = detect_openvpn_ip(interfaces)

    print("\n[*] Обнаружение сетевых интерфейсов:", flush=True)
    for ip, name in interfaces:
        tag = " <--- (Корпоративный туннель OpenVPN)" if ip == detected_ip else ""
        print(f"   * [{name}] : {ip}{tag}", flush=True)

    # 2. Проверка связи с ИИ
    print("\n[*] Проверка связи с серверами Google AI (через Amnezia)...", flush=True)
    ai_ok, ai_msg = check_upstream_ai()
    if ai_ok:
        print(f"   [УСПЕШНО] Серверы Google AI доступны! ({ai_msg})", flush=True)
    else:
        print(f"   [!] ВНИМАНИЕ: Не удалось связаться с Google AI: {ai_msg}", flush=True)
        print("   Убедитесь, что Amnezia запущена и подключена на этом домашнем ПК.", flush=True)

    # 3. Настройка порта
    port = DEFAULT_PORT
    listen_ip = "0.0.0.0"

    print(f"\n[+] Прокси запущен на 0.0.0.0:{port} (Доступен для рабочей машины: {detected_ip}:{port})", flush=True)
    print("-" * 76, flush=True)
    print(">>> ИНСТРУКЦИЯ ДЛЯ РАБОЧЕЙ МАШИНЫ (выполнять внутри RDP-сессии):", flush=True)
    print("-" * 76, flush=True)
    print("1. Проверка связи в PowerShell на рабочей машине:", flush=True)
    print(f"   curl.exe -I -x http://{detected_ip}:{port} https://generativelanguage.googleapis.com\n", flush=True)
    print("2. Установка переменных окружения для Antigravity (PowerShell):", flush=True)
    print(f'   [System.Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://{detected_ip}:{port}", "User")', flush=True)
    print(f'   [System.Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://{detected_ip}:{port}", "User")', flush=True)
    print('   [System.Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1,*.tensor.ru,*.sbis.ru", "User")\n', flush=True)
    print("3. Настройка интерфейса Antigravity (Settings -> Ctrl+, -> поиск 'proxy'):", flush=True)
    print(f"   Http: Proxy  -->  http://{detected_ip}:{port}", flush=True)
    print("-" * 76, flush=True)
    print(">>> ЖУРНАЛ ЗАПРОСОВ (Для остановки нажмите Ctrl+C):", flush=True)
    print(f"{'ВРЕМЯ':<10} {'IP КЛИЕНТА':<15} | {'МЕТОД':<7} {'ЦЕЛЕВОЙ РЕСУРС':<45} | СТАТУС", flush=True)
    print("-" * 76, flush=True)

    server = ThreadedProxyServer(listen_ip, port)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n\nОстановка прокси-моста...", flush=True)
        server.stop()
        print("Прокси-мост успешно остановлен.", flush=True)

if __name__ == "__main__":
    main()
