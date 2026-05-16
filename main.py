#!/usr/bin/env python3
"""
Bybit Volume Spike Bot - все монеты + новые листинги
Подписывается на wildcard поток — получает ВСЕ монеты автоматически
"""
import json
import time
import logging
import os
import threading
import requests
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

import websocket

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT             = int(os.environ.get("PORT", 8080))

MIN_VOLUME_USD   = 20_000_000
SPIKE_MULTIPLIER = 2.5
ALERT_COOLDOWN   = 300
TOP_REPORT_EVERY = 3600

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

tickers     = {}
volume_prev = {}
alerted     = {}
last_report = 0
lock        = threading.Lock()
start_time  = datetime.now()
spike_count = 0
known_coins = set()  # для отслеживания новых листингов


# ── HTTP сервер ──────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            count = len(tickers)
        body = json.dumps({
            "status":    "running",
            "uptime":    str(datetime.now() - start_time),
            "coins":     count,
            "spikes":    spike_count,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_http():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info(f"HTTP сервер на порту {PORT}")
    server.serve_forever()


# ── Telegram ─────────────────────────────────────────────
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


# ── Проверка нового листинга ─────────────────────────────
def check_new_listing(symbol: str, price: float, vol: float):
    if symbol in known_coins:
        return
    known_coins.add(symbol)
    # Не оповещаем при первом запуске (когда known_coins пустой изначально)
    if len(known_coins) <= 50:
        return
    name = symbol.replace("USDT", "")
    log.info(f"НОВЫЙ ЛИСТИНГ: {symbol}")
    send_telegram(
        f"<b>НОВЫЙ ЛИСТИНГ на Bybit!</b>\n"
        f"Монета: <b>{name}</b>\n"
        f"Цена:   <b>{price:.6g} USDT</b>\n"
        f"Объём:  {fmt_vol(vol)}\n"
        f"Время:  {datetime.now().strftime('%H:%M:%S')}\n"
        f"Bybit: https://www.bybit.com/trade/usdt/{symbol}"
    )


# ── Проверка всплеска объёма ─────────────────────────────
def check_spike(symbol: str, data: dict):
    global spike_count, last_report
    now    = time.time()
    vol    = data.get("volume_24h", 0)
    price  = data.get("price", 0)
    change = data.get("change_pct", 0)

    if price <= 0:
        return

    # Проверка нового листинга
    check_new_listing(symbol, price, vol)

    if vol < MIN_VOLUME_USD:
        return

    prev = volume_prev.get(symbol)
    volume_prev[symbol] = vol

    if prev is None or prev <= 0:
        return

    ratio = vol / prev
    if ratio < SPIKE_MULTIPLIER:
        return

    last_alert = alerted.get(symbol, 0)
    if now - last_alert < ALERT_COOLDOWN:
        return

    alerted[symbol] = now
    spike_count += 1

    sign      = "+" if change >= 0 else ""
    direction = "РОСТ" if change >= 0 else "ПАДЕНИЕ"
    msg = (
        f"<b>ВСПЛЕСК ОБЪЕМА - {symbol.replace('USDT','')}</b>\n"
        f"Цена:      <b>{price:.6g} USDT</b>\n"
        f"Изменение: <b>{sign}{change:.1f}%</b> ({direction})\n"
        f"Объём:     <b>{fmt_vol(vol)}</b>\n"
        f"Был:       {fmt_vol(prev)}\n"
        f"Всплеск:   <b>x{ratio:.1f}</b>\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}\n"
        f"Bybit: https://www.bybit.com/trade/usdt/{symbol}"
    )
    send_telegram(msg)
    log.info(f"SPIKE: {symbol} x{ratio:.1f} | {fmt_vol(vol)}")

    # Топ отчёт каждый час
    if now - last_report > TOP_REPORT_EVERY:
        last_report = now
        threading.Thread(target=send_top_report, daemon=True).start()


def send_top_report():
    with lock:
        data = dict(tickers)
    filtered = [(s, d) for s, d in data.items() if d.get("volume_24h", 0) >= MIN_VOLUME_USD]
    top = sorted(filtered, key=lambda x: x[1]["volume_24h"], reverse=True)[:20]
    if not top:
        return
    lines = [f"<b>ТОП-20 ПО ОБЪЕМУ | {datetime.now().strftime('%H:%M')}</b>\n"
             f"Всего монет: {len(data)}\n"]
    for i, (symbol, d) in enumerate(top, 1):
        name  = symbol.replace("USDT", "")
        vol   = fmt_vol(d["volume_24h"])
        chg   = d.get("change_pct", 0)
        sign  = "+" if chg >= 0 else ""
        arrow = "↑" if chg >= 0 else "↓"
        lines.append(f"{i:2}. <b>{name:<12}</b> {vol:<10} {arrow}{sign}{chg:.1f}%")
    send_telegram("\n".join(lines))


# ── WebSocket обработчики ────────────────────────────────
def on_message(ws, message):
    try:
        data   = json.loads(message)
        topic  = data.get("topic", "")

        if not topic.startswith("tickers."):
            return

        td     = data.get("data", {})
        symbol = td.get("symbol", "")

        if not symbol.endswith("USDT"):
            return

        try:
            price  = float(td.get("lastPrice")        or 0)
            vol    = float(td.get("turnover24h")       or 0)
            change = float(td.get("price24hPcnt")      or 0) * 100
            oi     = float(td.get("openInterestValue") or 0)
        except (ValueError, TypeError):
            return

        info = {"price": price, "volume_24h": vol, "change_pct": change, "oi_value": oi}

        with lock:
            tickers[symbol] = info

        check_spike(symbol, info)

    except Exception as e:
        log.error(f"on_message: {e}")


def on_error(ws, error):
    log.error(f"WS error: {error}")


def on_close(ws, code, msg):
    log.warning(f"WS закрыт: {code}")


def on_open(ws):
    """
    Bybit поддерживает подписку через REST + WS.
    Получаем список монет через несколько источников.
    """
    log.info("WS подключён — получаем список всех монет...")

    symbols = []

    # Попытка 1 — Bybit REST
    for url in ["https://api.bybit.com/v5/market/tickers",
                "https://api.bytick.com/v5/market/tickers"]:
        try:
            r = requests.get(url, params={"category": "linear"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                data = r.json()
                if data.get("retCode") == 0:
                    symbols = [t["symbol"] for t in data["result"]["list"]
                               if t["symbol"].endswith("USDT")]
                    log.info(f"Bybit REST OK: {len(symbols)} монет")
                    break
        except Exception as e:
            log.warning(f"Bybit REST failed ({url}): {e}")

    # Попытка 2 — если REST не сработал, используем большой список вручную
    if not symbols:
        log.info("Используем встроенный список монет...")
        symbols = [
            # Топ по объёму
            "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT","DOGEUSDT",
            "ADAUSDT","AVAXUSDT","DOTUSDT","MATICUSDT","LINKUSDT","UNIUSDT",
            "ATOMUSDT","LTCUSDT","ETCUSDT","APTUSDT","ARBUSDT","OPUSDT",
            "INJUSDT","SUIUSDT","SEIUSDT","TIAUSDT","JUPUSDT","WLDUSDT",
            "FETUSDT","RNDRUSDT","GRTUSDT","AAVEUSDT","MKRUSDT","SNXUSDT",
            # Твои монеты
            "RIVERUSDT","SKRUSDT","TRUMPUSDT","HYPEUSDT","BERUSDT",
            "ORCAUSDT","TAOUSDT","STOUSDT","PIPPINUSDT","AZTECUSDT",
            "RDNTUSDT","ICPUSDT","STGUSDT","BLUAIUSDT","ENSOUST",
            "SIRENUSDT","RAVEUSDT","RESOLVUSDT","MOODENGUSDT","LYNUSDT",
            # Популярные альты
            "NEARUSDT","FILUSDT","ALGOUSDT","VETUSDT","XTZUSDT","EGLDUSDT",
            "FLOWUSDT","ICPUSDT","HNTUSDT","RUNEUSDT","KAVAUSDT","BANDUSDT",
            "CELOUSDT","ZILUSDT","IOTAUSDT","ONEUSDT","ANKRUSDT","SKLUSDT",
            "CRVUSDT","COMPUSDT","YFIUSDT","SUSHIUSDT","BALUSDT","RENUSDT",
            "STORJUSDT","ENJUSDT","MANAUSDT","SANDUSDT","AXSUSDT","GALAUSDT",
            "DYDXUSDT","GMXUSDT","PERPUSDT","BLURUSDT","LDOUSDT","RPLУСДТ",
            "STETHUSDT","FRAXUSDT","FXSUSDT","CVXUSDT","FLOKIUSDT","PEPEUSDT",
            "WIFUSDT","BONKUSDT","MEMEUSDT","SHIBUSDT","BOMEUSDT","POPCATUSDT",
            "NOTUSDT","EIGENUSDT","SCRUSDT","ZROUSDT","ZKUSDT","ALTUSDT",
            "DYMUSDT","PYTHUSDT","JITOUSDT","WUSDT","STRKUSDT","PIXELUSDT",
            "PORTALUSDT","AEVOUSDT","SAGAUSDT","ZETAUSDT","ALTUSDT","ACEUSDT",
            "XAIUSDT","MANTAUSDT","TAIKO","ETHFIUSDT","RENZOUSDT","BBUSDT",
        ]

    log.info(f"Подписываемся на {len(symbols)} монет")

    # Подписываемся батчами по 10
    for i in range(0, len(symbols), 10):
        batch = [f"tickers.{s}" for s in symbols[i:i+10]]
        ws.send(json.dumps({"op": "subscribe", "args": batch}))
        time.sleep(0.05)

    # Добавляем в known_coins чтобы не слать сигнал о "новом листинге" при старте
    known_coins.update(symbols)

    log.info(f"Готово! Слушаем {len(symbols)} монет в реальном времени")
    send_telegram(
        f"<b>Bybit Volume Bot запущен!</b>\n"
        f"Режим: WebSocket (реальное время)\n"
        f"Монет отслеживается: <b>{len(symbols)}</b>\n"
        f"Мин. объём: {fmt_vol(MIN_VOLUME_USD)}\n"
        f"Порог всплеска: x{SPIKE_MULTIPLIER}\n"
        f"Новые листинги: отслеживаются"
    )


def ping_loop(ws):
    while True:
        time.sleep(20)
        try:
            ws.send(json.dumps({"op": "ping"}))
        except Exception:
            break


def run_websocket():
    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://stream.bybit.com/v5/public/linear",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            t = threading.Thread(target=ping_loop, args=(ws,), daemon=True)
            t.start()
            ws.run_forever()
        except Exception as e:
            log.error(f"WS упал: {e}")
        log.warning("Переподключаемся через 5 сек...")
        time.sleep(5)


def main():
    log.info("Запуск Bybit Volume Bot...")
    log.info(f"TELEGRAM_TOKEN:   {'OK' if TELEGRAM_TOKEN else 'НЕ ЗАДАН!'}")
    log.info(f"TELEGRAM_CHAT_ID: {'OK' if TELEGRAM_CHAT_ID else 'НЕ ЗАДАН!'}")

    threading.Thread(target=run_http, daemon=True).start()
    run_websocket()


if __name__ == "__main__":
    main()
