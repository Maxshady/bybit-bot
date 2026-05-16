#!/usr/bin/env python3
"""
Bybit Volume Spike Bot - WebSocket версия
Получает данные в реальном времени через WebSocket
"""
import json
import time
import logging
import os
import threading
import requests
from datetime import datetime
from collections import defaultdict

import websocket

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_VOLUME_USD   = 20_000_000   # минимальный объём 20 млн $
SPIKE_MULTIPLIER = 2.5          # всплеск = рост объёма в X раз
ALERT_COOLDOWN   = 300          # антиспам — не повторять сигнал N секунд
TOP_REPORT_EVERY = 3600         # топ отчёт каждые N секунд (1 час)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# Хранилище данных по монетам
tickers = {}          # symbol -> последние данные
volume_prev = {}      # symbol -> предыдущий объём (для расчёта всплеска)
alerted = {}          # symbol -> время последнего сигнала
last_report = 0       # время последнего топ-отчёта
lock = threading.Lock()


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15
        )
        if r.ok:
            log.info("Telegram: отправлено")
        else:
            log.error(f"Telegram: {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


def fmt_vol(v: float) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B$"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M$"
    return f"{v:,.0f}$"


def check_spike(symbol: str, data: dict):
    """Проверить всплеск объёма для монеты"""
    global last_report

    now = time.time()
    vol = data.get("volume_24h", 0)
    price = data.get("price", 0)
    change = data.get("change_pct", 0)

    if vol < MIN_VOLUME_USD or price <= 0:
        return

    prev = volume_prev.get(symbol)
    volume_prev[symbol] = vol

    if prev is None or prev <= 0:
        return

    ratio = vol / prev
    if ratio < SPIKE_MULTIPLIER:
        return

    # Антиспам
    last_alert = alerted.get(symbol, 0)
    if now - last_alert < ALERT_COOLDOWN:
        return

    alerted[symbol] = now
    sign = "+" if change >= 0 else ""
    direction = "РОСТ" if change >= 0 else "ПАДЕНИЕ"

    msg = (
        f"<b>ВСПЛЕСК ОБЪЕМА - {symbol.replace('USDT','')}</b>\n"
        f"Цена:      <b>{price:.6g} USDT</b>\n"
        f"Изменение: <b>{sign}{change:.1f}%</b> ({direction})\n"
        f"Объем:     <b>{fmt_vol(vol)}</b>\n"
        f"Был:       {fmt_vol(prev)}\n"
        f"Всплеск:   <b>x{ratio:.1f}</b>\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram(msg)
    log.info(f"SPIKE: {symbol} | x{ratio:.1f} | {fmt_vol(vol)}")


def send_top_report():
    """Топ монет по объёму"""
    with lock:
        data = dict(tickers)

    filtered = [(s, d) for s, d in data.items() if d.get("volume_24h", 0) >= MIN_VOLUME_USD]
    top = sorted(filtered, key=lambda x: x[1]["volume_24h"], reverse=True)[:20]

    if not top:
        return

    lines = [f"<b>ТОП-20 ПО ОБЪЕМУ | {datetime.now().strftime('%H:%M')}</b>\n"]
    for i, (symbol, d) in enumerate(top, 1):
        name   = symbol.replace("USDT", "")
        vol    = fmt_vol(d["volume_24h"])
        chg    = d.get("change_pct", 0)
        sign   = "+" if chg >= 0 else ""
        arrow  = "↑" if chg >= 0 else "↓"
        lines.append(f"{i:2}. <b>{name:<12}</b> {vol:<10} {arrow}{sign}{chg:.1f}%")

    send_telegram("\n".join(lines))


def on_message(ws, message):
    """Обработка сообщений WebSocket"""
    global last_report
    try:
        data = json.loads(message)

        # Пинг-понг
        if data.get("op") == "ping" or data.get("ret_msg") == "pong":
            return

        topic = data.get("topic", "")
        if not topic.startswith("tickers."):
            return

        ticker_data = data.get("data", {})
        symbol = ticker_data.get("symbol", "")

        if not symbol.endswith("USDT"):
            return

        # Достаём нужные поля
        try:
            price      = float(ticker_data.get("lastPrice")        or 0)
            vol_24h    = float(ticker_data.get("turnover24h")       or 0)
            change_pct = float(ticker_data.get("price24hPcnt")      or 0) * 100
            oi_value   = float(ticker_data.get("openInterestValue") or 0)
        except (ValueError, TypeError):
            return

        if price <= 0:
            return

        ticker_info = {
            "price":      price,
            "volume_24h": vol_24h,
            "change_pct": change_pct,
            "oi_value":   oi_value,
            "updated":    time.time(),
        }

        with lock:
            tickers[symbol] = ticker_info

        # Проверяем всплеск
        check_spike(symbol, ticker_info)

        # Топ отчёт каждый час
        now = time.time()
        if now - last_report > TOP_REPORT_EVERY:
            last_report = now
            threading.Thread(target=send_top_report, daemon=True).start()

    except Exception as e:
        log.error(f"on_message error: {e}")


def on_error(ws, error):
    log.error(f"WebSocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    log.warning(f"WebSocket закрыт: {close_status_code} {close_msg}")


def on_open(ws):
    """Подписываемся на все тикеры при открытии соединения"""
    log.info("WebSocket подключён — подписываемся на тикеры...")

    # Сначала получаем список всех USDT монет
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear"},
            timeout=15
        )
        data = r.json()
        symbols = [
            t["symbol"] for t in data["result"]["list"]
            if t["symbol"].endswith("USDT")
        ]
        log.info(f"Найдено {len(symbols)} монет")
    except Exception as e:
        log.error(f"Не удалось получить список монет: {e}")
        # Подписываемся на популярные монеты вручную
        symbols = [
            "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
            "DOGEUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","MATICUSDT",
            "LINKUSDT","UNIUSDT","ATOMUSDT","LTCUSDT","ETCUSDT",
        ]

    # WebSocket принимает максимум 10 символов за раз
    batch_size = 10
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        args  = [f"tickers.{s}" for s in batch]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        time.sleep(0.1)

    log.info("Подписка оформлена — слушаем рынок в реальном времени")
    send_telegram(
        f"<b>Bybit Volume Bot запущен (WebSocket)</b>\n"
        f"Режим: реальное время\n"
        f"Монет отслеживается: {len(symbols)}\n"
        f"Мин. объём: {fmt_vol(MIN_VOLUME_USD)}\n"
        f"Порог всплеска: x{SPIKE_MULTIPLIER}"
    )


def ping_loop(ws):
    """Держим соединение живым пингами"""
    while True:
        time.sleep(20)
        try:
            ws.send(json.dumps({"op": "ping"}))
        except Exception:
            break


def run_websocket():
    """Запуск WebSocket с автопереподключением"""
    url = "wss://stream.bybit.com/v5/public/linear"
    while True:
        try:
            log.info(f"Подключаемся к {url}...")
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            # Пинг в отдельном потоке
            ping_thread = threading.Thread(target=ping_loop, args=(ws,), daemon=True)
            ping_thread.start()

            ws.run_forever(ping_interval=30, ping_timeout=10)

        except Exception as e:
            log.error(f"WebSocket упал: {e}")

        log.warning("Переподключаемся через 5 секунд...")
        time.sleep(5)


def main():
    log.info("Bybit Volume Bot (WebSocket) запускается...")
    log.info(f"TELEGRAM_TOKEN:   {'OK' if TELEGRAM_TOKEN else 'НЕ ЗАДАН!'}")
    log.info(f"TELEGRAM_CHAT_ID: {'OK' if TELEGRAM_CHAT_ID else 'НЕ ЗАДАН!'}")
    run_websocket()


if __name__ == "__main__":
    main()
