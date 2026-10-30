---
name: work-ai-bridge
description: Manages the local AI proxy bridge between Home PC (running OpenVPN and Amnezia) and Work PC (running Antigravity via RDP). Use this skill when starting, configuring, or troubleshooting the network proxy bridge to enable Antigravity or AI tools from a restricted corporate work machine.
---

# Work-AI Bridge: Прокси-мост для работы Antigravity на рабочей машине

Данный скилл описывает процесс организации безопасного сетевого моста между **домашним ПК** (на котором работают OpenVPN и Amnezia) и **рабочей машиной** (доступной по RDP, без прав администратора), чтобы запускать на ней Antigravity без риска разрыва сессии RDP и без нарушения корпоративной безопасности.

## Архитектура решения

```mermaid
flowchart LR
    subgraph HomePC ["Домашний ПК"]
        Bridge["Bridge Proxy\n(:8080)"]
        Amnezia["AmneziaVPN\n(Зарубежный шлюз)"]
        HomeOpenVPN["OpenVPN Client\n(10.206.147.127)"]
        Bridge --> Amnezia
    end

    subgraph WorkPC ["Рабочая машина (RDP)"]
        Antigravity["Antigravity IDE / Agent\nHTTP_PROXY=:8080"]
        WorkNet["Корпоративная сеть\n"]
        Antigravity -. "AI трафик" .-> HomeOpenVPN
        Antigravity -- "NO_PROXY (напрямую)" --> WorkNet
    end

    HomeOpenVPN <== "Корпоративный OpenVPN туннель" ==> WorkPC
    Amnezia ==> "Интернет" ==> GoogleAI["Google Gemini / AI API"]
```

---

## 1. Запуск моста на домашнем ПК

Для запуска сервера-моста используется скрипт [bridge_proxy.py](scripts/bridge_proxy.py), не требующий внешних зависимостей.

### Запуск через консоль:
```powershell
python bridge_proxy.py
```
*(Или запустите двойным кликом файл [run_bridge.bat](scripts/run_bridge.bat))*

### Что делает скрипт:
1. Автоматически находит IP-адрес домашнего ПК в подсети OpenVPN (например, `10.206.147.127`).
2. Проверяет доступность серверов Google AI через Amnezia.
3. Запускает многопоточный HTTP/HTTPS CONNECT прокси на порту `8080`.
4. Выводит готовые команды для вставки на рабочей машине.
5. Логирует все входящие запросы в реальном времени.

---

## 2. Настройка рабочей машины (через RDP)

На рабочей машине сетевой шлюз и настройки адаптеров **НЕ меняются**. Настраивается только доступ для Antigravity.

### Шаг 2.1. Проверка связи
В PowerShell на рабочей машине выполните:
```powershell
curl.exe -I -x http://10.206.147.127:8080 https://generativelanguage.googleapis.com
```
*Ожидаемый ответ*: `HTTP/1.1 200 Connection Established`.

### Шаг 2.2. Установка переменных окружения пользователя
Выполните в PowerShell (права администратора **НЕ нужны**):
```powershell
[System.Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://10.206.147.127:8080", "User")
[System.Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://10.206.147.127:8080", "User")
[System.Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1", "User")
```

### Шаг 2.3. Настройка интерфейса Antigravity
1. Откройте **Settings** (`Ctrl + ,`).
2. В строке поиска введите `proxy`.
3. Укажите:
   * **Http: Proxy**: `http://10.206.147.127:8080`
   * **Http: Proxy Support**: `override` (или `on`)

---

## 3. Безопасность данных (DLP и корпоративная тайна)

1. **Исключение корпоративных доменов**: Маска `NO_PROXY=localhost,127.0.0.1` гарантирует, что запросы к внутренним git-репозиториям, стендам и базам данных идут напрямую по внутренней сети и не проходят через домашний ПК.
2. **Только служебный трафик**: Через мост идут только шифрованные HTTPS-сессии Antigravity к API (`generativelanguage.googleapis.com` и др.).

---

## 4. Диагностика и решение проблем

| Симптом | Причина | Решение |
| :--- | :--- | :--- |
| `Connection timed out` при тесте curl | Брандмауэр Windows на домашнем ПК блокирует порт 8080 | При первом запуске скрипта разрешить доступ Python для Частных/Общественных сетей, либо добавить входящее правило TCP 8080 в Windows Firewall. |
| `HTTP 502 Bad Gateway` | Домашний ПК не может достучаться до Google | Проверьте подключение Amnezia на домашнем ПК. |
| Сменился IP-адрес | OpenVPN переподключился с новым IP | Запустите `bridge_proxy.py` — он покажет актуальный IP в строке Network Discovery. Обновите порт/IP в Antigravity. |
