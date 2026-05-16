#!/usr/bin/env python3
import time
import requests
import logging
import os
from datetime import datetime
from collections import defaultdict
 
# Читаем токены из переменных окружения Railway
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
 
MIN_VOLUME_USD   = 20_000_000
SPIKE_MULTIPLIER = 2.5
CHECK_INTERVAL   = 60
HISTORY_PERIODS  = 10
TOP_COINS_COUNT  = 20
ALERT_COOLDOWN   = 300
 
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)
 
volume_history = defaultdict(list)
alerted_coins  = {}
 
 
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Токены не заданы!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=15)
        if r.ok:
            log.info("Telegram: сообщение отправлено")
        else:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
 
 
def get_bybit_tickers():
    """Получить тикеры через v5 API Bybit"""
    url = "https://api.bybit.com/v5/market/tickers"
    headers = {"Accept": "application/json"}
    try:
        r = requests.get(
            url,
            params={"category": "linear"},
            headers=headers,
            timeout=15
        )
        # Проверяем что ответ — валидный JSON
        data = r.json()
        if data.get("retCode") != 0:
            log.error(f"Bybit API error: {data.get('retMsg')}")
            return []
        tickers = data["result"]["list"]
        log.info(f"Получено {len(tickers)} тикеров от Bybit")
        return tickers
    except requests.exceptions.JSONDecodeError as e:
        log.error(f"Bybit вернул не JSON: {e}")
        return []
    except Exception as e:
        log.error(f"Bybit request failed: {e}")
        return []
 
 
def parse_ticker(t: dict):
    symbol = t.get("symbol", "")
    if not symbol.endswith("USDT"):
        return None
    try:
        volume_24h = float(t.get("turnover24h") or 0)
        price      = float(t.get("lastPrice")   or 0)
        change_pct = float(t.get("price24hPcnt") or 0) * 100
        oi_value   = float(t.get("openInterestValue") or 0)
    except (ValueError, TypeError):
        return None
    if price <= 0:
        return None
    return {
        "symbol":     symbol,
        "price":      price,
        "volume_24h": volume_24h,
        "change_pct": change_pct,
        "oi_value":   oi_value,
    }
 
 
def fmt_volume(v: float) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B$"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M$"
    return f"{v:,.0f}$"
 
 
def check_spikes(tickers):
    spikes = []
    now = time.time()
    for t in tickers:
        symbol = t["symbol"]
        vol    = t["volume_24h"]
        volume_history[symbol].append(vol)
        if len(volume_history[symbol]) > HISTORY_PERIODS:
            volume_history[symbol].pop(0)
        if len(volume_history[symbol]) < 3:
            continue
        history = volume_history[symbol][:-1]
        avg_vol = sum(history) / len(history)
        if avg_vol <= 0:
            continue
        ratio = vol / avg_vol
        if vol >= MIN_VOLUME_USD and ratio >= SPIKE_MULTIPLIER:
            last_alert = alerted_coins.get(symbol, 0)
            if now - last_alert < ALERT_COOLDOWN:
                continue
            alerted_coins[symbol] = now
            spikes.append({**t, "avg_vol": avg_vol, "ratio": ratio})
    return spikes
 
 
def send_spike_alert(spike: dict):
    symbol     = spike["symbol"].replace("USDT", "")
    price      = spike["price"]
    vol        = spike["volume_24h"]
    avg_vol    = spike["avg_vol"]
    ratio      = spike["ratio"]
    change_pct = spike["change_pct"]
    oi_value   = spike["oi_value"]
    change_sign = "+" if change_pct >= 0 else ""
    direction   = "RОСТ" if change_pct >= 0 else "ПАДЕНИЕ"
 
    msg = (
        f"<b>ВСПЛЕСК ОБЪЕМА - {symbol}</b>\n"
        f"Цена:      <b>{price:.6g} USDT</b>\n"
        f"Изменение: <b>{change_sign}{change_pct:.1f}%</b> ({direction})\n"
        f"Объем 24ч: <b>{fmt_volume(vol)}</b>\n"
        f"Средний:   {fmt_volume(avg_vol)}\n"
        f"Всплеск:   <b>x{ratio:.1f}</b>\n"
        f"ОИ:        {fmt_volume(oi_value)}\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram(msg)
    log.info(f"SPIKE: {symbol} | vol={fmt_volume(vol)} | x{ratio:.1f}")
 
 
def send_top_report(tickers):
    filtered = [t for t in tickers if t["volume_24h"] >= MIN_VOLUME_USD]
    top = sorted(filtered, key=lambda x: x["volume_24h"], reverse=True)[:TOP_COINS_COUNT]
    if not top:
        return
    lines = [f"<b>ТОП-{TOP_COINS_COUNT} ПО ОБЪЕМУ | {datetime.now().strftime('%H:%M')}</b>\n"]
    for i, t in enumerate(top, 1):
        symbol = t["symbol"].replace("USDT", "")
        vol    = fmt_volume(t["volume_24h"])
        chg    = t["change_pct"]
        sign   = "+" if chg >= 0 else ""
        arrow  = "↑" if chg >= 0 else "↓"
        lines.append(f"{i:2}. <b>{symbol:<12}</b> {vol:<10} {arrow}{sign}{chg:.1f}%")
    send_telegram("\n".join(lines))
 
 
def main():
    log.info("Bybit Volume Bot запущен")
    log.info(f"TELEGRAM_TOKEN:   {'OK' if TELEGRAM_TOKEN else 'НЕ ЗАДАН!'}")
    log.info(f"TELEGRAM_CHAT_ID: {'OK' if TELEGRAM_CHAT_ID else 'НЕ ЗАДАН!'}")
    log.info(f"Мин. объём: {fmt_volume(MIN_VOLUME_USD)}")
    log.info(f"Порог всплеска: x{SPIKE_MULTIPLIER}")
 
    send_telegram(
        f"<b>Bybit Volume Bot запущен!</b>\n"
        f"Мин. объём: {fmt_volume(MIN_VOLUME_USD)}\n"
        f"Порог всплеска: x{SPIKE_MULTIPLIER}\n"
        f"Проверка каждые {CHECK_INTERVAL} сек\n"
        f"Жду данные для калибровки (~{HISTORY_PERIODS} мин)..."
    )
 
    iteration = 0
    REPORT_EVERY = 60
 
    while True:
        try:
            raw_tickers = get_bybit_tickers()
            if not raw_tickers:
                time.sleep(CHECK_INTERVAL)
                continue
 
            tickers = [p for t in raw_tickers if (p := parse_ticker(t))]
 
            spikes = check_spikes(tickers)
            for spike in spikes:
                send_spike_alert(spike)
 
            iteration += 1
            if iteration % REPORT_EVERY == 0:
                send_top_report(tickers)
 
            time.sleep(CHECK_INTERVAL)
 
        except KeyboardInterrupt:
            log.info("Остановлено")
            break
        except Exception as e:
            log.error(f"Ошибка: {e}")
            time.sleep(30)
 
 
if __name__ == "__main__":
    main()
