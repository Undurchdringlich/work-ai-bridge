---
name: work-ai-bridge
description: Manages the local AI proxy bridge between Home PC (running OpenVPN and Amnezia) and Work PC (running Antigravity via RDP). Use this skill when starting, configuring, or troubleshooting the network proxy bridge to enable Antigravity or AI tools from a restricted corporate work machine.
---

# Work-AI Bridge: Прокси-мост для работы Antigravity на рабочей машине

Данный скилл описывает процесс организации безопасного сетевого моста между **домашним ПК** (на котором работают OpenVPN и AmneziaVPN / зарубежный интернет) и **рабочей машиной** (доступной по RDP, без прав администратора), чтобы запускать на ней Antigravity без риска разрыва сессии RDP и без нарушения корпоративной безопасности.

## Архитектура решения

```mermaid
flowchart LR
    subgraph HomePC ["Домашний ПК"]
        Bridge["Bridge Proxy\n(:8080)"]
        Amnezia["AmneziaVPN / Egress\n(Свободный интернет)"]
        HomeOpenVPN["OpenVPN Client\n(<HOME_VPN_IP>)"]
        Bridge --> Amnezia
    end

    subgraph WorkPC ["Рабочая машина (RDP)"]
        Antigravity["Antigravity IDE / Agent\nHTTP_PROXY=:8080"]
        WorkNet["Корпоративная сеть\n(GitLab, Jira, стенды)"]
        Antigravity -. "AI трафик" .-> HomeOpenVPN
        Antigravity -- "NO_PROXY (напрямую)" --> WorkNet
    end

    HomeOpenVPN <== "Корпоративный VPN-туннель" ==> WorkPC
    Amnezia ==> "Интернет" ==> GoogleAI["Google Gemini / AI API"]
```

---

## 1. Запуск моста на домашнем ПК

Для запуска сервера-моста используется скрипт [bridge_proxy.py](scripts/bridge_proxy.py), не требующий сторонних библиотек (только стандартная библиотека Python 3.8+).

### Запуск через консоль:
```powershell
python scripts/bridge_proxy.py
```
*(Или запустите двойным кликом файл [run_bridge.bat](run_bridge.bat) в корне репозитория)*

### Дополнительные параметры (опционально):
```powershell
# Указать кастомный порт и корпоративные домены для NO_PROXY:
python scripts/bridge_proxy.py --port 8080 --corp-domains "*.company.com,*.corp.internal"
```

### Что делает скрипт:
1. Автоматически определяет IP-адрес домашнего ПК в подсети корпоративного OpenVPN (например, `10.x.x.x`).
2. Проверяет доступность upstream AI серверов (Google Gemini API) через шлюз домашнего интернета / Amnezia.
3. Запускает многопоточный HTTP/HTTPS CONNECT прокси на выбранном порту (по умолчанию `8080`).
4. Выводит готовые команды PowerShell с реальным обнаруженным IP-адресом для быстрой вставки на рабочей машине.
5. Логирует все входящие запросы в реальном времени.

---

## 2. Настройка рабочей машины (через RDP)

На рабочей машине сетевой шлюз и настройки сетевых адаптеров **НЕ меняются**. Настраивается только доступ для Antigravity и консольных AI-инструментов. Права администратора **НЕ требуются**.

> [!NOTE]
> В командах ниже замените `<HOME_VPN_IP>` на реальный IP-адрес домашнего ПК, который `bridge_proxy.py` вывел в консоли при запуске (например, `10.x.x.x`), а порт `8080` — на выбранный порт.

### Шаг 2.1. Проверка связи
В PowerShell на рабочей машине выполните:
```powershell
curl.exe -I -x http://<HOME_VPN_IP>:8080 https://generativelanguage.googleapis.com
```
*Ожидаемый ответ*: `HTTP/1.1 200 Connection Established`.

### Шаг 2.2. Установка переменных окружения пользователя
Выполните в PowerShell на рабочей машине (действует для текущего пользователя, права админа не нужны):
```powershell
[System.Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://<HOME_VPN_IP>:8080", "User")
[System.Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://<HOME_VPN_IP>:8080", "User")
[System.Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1,*.corp.yourcompany.com", "User")
```
*(Замените `*.corp.yourcompany.com` на маски внутренних доменов вашей организации).*

### Шаг 2.3. Настройка интерфейса Antigravity IDE
1. Откройте **Settings** (`Ctrl + ,`).
2. В строке поиска введите `proxy`.
3. Укажите:
   * **Http: Proxy**: `http://<HOME_VPN_IP>:8080`
   * **Http: Proxy Support**: `override` (или `on`)

---

## 3. Безопасность данных (DLP и корпоративная изоляция)

1. **Исключение корпоративных доменов**: Маска `NO_PROXY=localhost,127.0.0.1,*.company.com` гарантирует, что запросы к корпоративным git-репозиториям, внутренним сайтам, трекерам задач и базам данных идут напрямую через корпоративную сеть и не попадают на домашний ПК.
2. **Только служебный AI трафик**: Через мост идут только шифрованные HTTPS-сессии Antigravity к API нейросетей (`generativelanguage.googleapis.com` и др.).
3. **End-to-End шифрование**: Метод `CONNECT` осуществляет прозрачное туннелирование TLS. Прокси-сервер не расшифровывает и не подменяет SSL-сертификаты.

---

## 4. Диагностика и решение проблем

| Симптом | Причина | Решение |
| :--- | :--- | :--- |
| `Connection timed out` при тесте curl | Брандмауэр Windows на домашнем ПК блокирует порт | Разрешить входящий трафик для Python в Windows Firewall (Частные сети) или выполнить на домашнем ПК: `netsh advfirewall firewall add rule name="WorkAIBridge" dir=in action=allow protocol=TCP localport=8080`. |
| `HTTP 502 Bad Gateway` | Домашний ПК не может достучаться до AI API | Проверьте подключение AmneziaVPN или домашнего интернет-соединения на домашнем ПК. |
| Сменился IP-адрес | OpenVPN переподключился с новым IP | Запустите `bridge_proxy.py` — он покажет актуальный IP в консоли. Обновите порт/IP в Antigravity. |
| Обрыв во время долгих ответов модели | Сброс соединения по неактивности | В скрипт встроен таймаут в 30 минут и TCP KeepAlive. Убедитесь, что используется актуальная версия `bridge_proxy.py`. |
