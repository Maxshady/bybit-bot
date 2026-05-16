#!/usr/bin/env python3
import time
import requests
import logging
import os
from datetime import datetime
from collections import defaultdict

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
            log.info("Telegram: отправлено")
        else:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


def get_bybit_tickers():
    """
    Пробуем несколько способов получить данные Bybit.
    1. Прямой API
    2. Через CoinGecko (открытый, не блокирует)
    """

    # Способ 1 — прямой Bybit с разными заголовками
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "python-requests/2.31.0",
        "Cache-Control": "no-cache",
    }
    for url in [
        "https://api.bybit.com/v5/market/tickers",
        "https://api.bytick.com/v5/market/tickers",
        "https://api2.bybit.com/v5/market/tickers",
    ]:
        try:
            r = requests.get(url, params={"category": "linear"}, headers=headers, timeout=20)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                data = r.json()
                if data.get("retCode") == 0:
                    tickers = data["result"]["list"]
                    log.info(f"Bybit OK: {len(tickers)} тикеров ({url})")
                    return tickers, "bybit"
        except Exception as e:
            log.warning(f"Bybit {url} failed: {e}")

    # Способ 2 — CoinGecko (бесплатный открытый API)
    log.info("Пробую CoinGecko...")
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": False,
            },
            timeout=20
        )
        if r.status_code == 200:
            coins = r.json()
            log.info(f"CoinGecko OK: {len(coins)} монет")
            return coins, "coingecko"
    except Exception as e:
        log.error(f"CoinGecko failed: {e}")

    return [], None


def parse_bybit_ticker(t: dict):
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
    return {"symbol": symbol, "price": price, "volume_24h": volume_24h,
            "change_pct": change_pct, "oi_value": oi_value}


def parse_coingecko_ticker(t: dict):
    symbol = (t.get("symbol") or "").upper() + "USDT"
    try:
        volume_24h = float(t.get("total_volume") or 0)
        price      = float(t.get("current_price") or 0)
        change_pct = float(t.get("price_change_percentage_24h") or 0)
    except (ValueError, TypeError):
        return None
    if price <= 0 or volume_24h <= 0:
        return None
    return {"symbol": symbol, "price": price, "volume_24h": volume_24h,
            "change_pct": change_pct, "oi_value": 0}


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
    symbol      = spike["symbol"].replace("USDT", "")
    change_sign = "+" if spike["change_pct"] >= 0 else ""
    direction   = "РОСТ" if spike["change_pct"] >= 0 else "ПАДЕНИЕ"
    msg = (
        f"<b>ВСПЛЕСК ОБЪЕМА - {symbol}</b>\n"
        f"Цена:      <b>{spike['price']:.6g} USDT</b>\n"
        f"Изменение: <b>{change_sign}{spike['change_pct']:.1f}%</b> ({direction})\n"
        f"Объем 24ч: <b>{fmt_volume(spike['volume_24h'])}</b>\n"
        f"Средний:   {fmt_volume(spike['avg_vol'])}\n"
        f"Всплеск:   <b>x{spike['ratio']:.1f}</b>\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram(msg)
    log.info(f"SPIKE: {symbol} | x{spike['ratio']:.1f}")


def send_top_report(tickers, source):
    filtered = [t for t in tickers if t["volume_24h"] >= MIN_VOLUME_USD]
    top = sorted(filtered, key=lambda x: x["volume_24h"], reverse=True)[:TOP_COINS_COUNT]
    if not top:
        return
    src = "Bybit" if source == "bybit" else "CoinGecko"
    lines = [f"<b>ТОП-{TOP_COINS_COUNT} ПО ОБЪЕМУ | {datetime.now().strftime('%H:%M')} ({src})</b>\n"]
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

    send_telegram(
        f"<b>Bybit Volume Bot запущен!</b>\n"
        f"Мин. объём: {fmt_volume(MIN_VOLUME_USD)}\n"
        f"Порог всплеска: x{SPIKE_MULTIPLIER}\n"
        f"Проверка каждые {CHECK_INTERVAL} сек"
    )

    iteration    = 0
    REPORT_EVERY = 60
    last_source  = None

    while True:
        try:
            raw_data, source = get_bybit_tickers()

            if not raw_data:
                log.error("Нет данных ни от одного источника")
                time.sleep(CHECK_INTERVAL)
                continue

            # Если источник сменился — уведомить
            if source != last_source:
                src_name = "Bybit" if source == "bybit" else "CoinGecko (резерв)"
                send_telegram(f"Источник данных: <b>{src_name}</b>")
                last_source = source

            # Парсим в зависимости от источника
            if source == "bybit":
                tickers = [p for t in raw_data if (p := parse_bybit_ticker(t))]
            else:
                tickers = [p for t in raw_data if (p := parse_coingecko_ticker(t))]

            log.info(f"Монет после фильтра: {len(tickers)}")

            spikes = check_spikes(tickers)
            for spike in spikes:
                send_spike_alert(spike)

            iteration += 1
            if iteration % REPORT_EVERY == 0:
                send_top_report(tickers, source)

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Ошибка: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
