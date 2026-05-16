#!/usr/bin/env python3
"""
Bybit Volume Spike Bot
Отслеживает всплески объёмов на Bybit и присылает сигналы в Telegram
Автор: для Максима, Зеленоград
"""

import time
import requests
import logging
from datetime import datetime
from collections import defaultdict

# ══════════════════════════════════════════════
#  НАСТРОЙКИ — заполни перед запуском
# ══════════════════════════════════════════════

TELEGRAM_TOKEN   = "8854640423:AAEbVQ9xXEq45Sp6BUsnJK2oJknmsT_9LzA"      # получи у @BotFather в Telegram
TELEGRAM_CHAT_ID = "486359182"          # получи у @userinfobot в Telegram

MIN_VOLUME_USD   = 20_000_000   # минимальный объём в $ (20 млн)
SPIKE_MULTIPLIER = 2.5          # всплеск = текущий объём в X раз больше среднего
CHECK_INTERVAL   = 60           # проверка каждые N секунд (60 = 1 минута)
HISTORY_PERIODS  = 10           # сколько периодов хранить для расчёта среднего
TOP_COINS_COUNT  = 20           # топ N монет по объёму в итоговом отчёте

# ══════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# История объёмов для каждой монеты
volume_history = defaultdict(list)
alerted_coins  = {}  # монеты по которым уже отправлен сигнал (антиспам)
ALERT_COOLDOWN = 300  # не повторять сигнал по одной монете N секунд


def send_telegram(message: str):
    """Отправить сообщение в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def get_bybit_tickers():
    """Получить все тикеры USDT перпетуал с Bybit"""
    url = "https://api.bybit.com/v5/market/tickers"
    try:
        r = requests.get(url, params={"category": "linear"}, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            log.error(f"Bybit API error: {data}")
            return []
        return data["result"]["list"]
    except Exception as e:
        log.error(f"Bybit request failed: {e}")
        return []


def get_open_interest_rank():
    """Получить топ монет по открытому интересу (прокси числа трейдеров)"""
    url = "https://api.bybit.com/v5/market/open-interest"
    # Открытый интерес уже есть в тикерах, используем его оттуда
    return {}


def parse_ticker(t: dict) -> dict | None:
    """Разобрать тикер и вернуть нужные поля"""
    symbol = t.get("symbol", "")
    if not symbol.endswith("USDT"):
        return None

    try:
        volume_24h = float(t.get("turnover24h", 0) or 0)   # объём в USDT
        price      = float(t.get("lastPrice",   0) or 0)
        change_pct = float(t.get("price24hPcnt",0) or 0) * 100
        oi         = float(t.get("openInterest",0) or 0)   # открытый интерес
        oi_value   = float(t.get("openInterestValue", 0) or 0)
    except (ValueError, TypeError):
        return None

    if price <= 0:
        return None

    return {
        "symbol":     symbol,
        "price":      price,
        "volume_24h": volume_24h,
        "change_pct": change_pct,
        "oi":         oi,
        "oi_value":   oi_value,
    }


def fmt_volume(v: float) -> str:
    """Форматировать объём"""
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B$"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M$"
    return f"{v:,.0f}$"


def check_spikes(tickers: list[dict]) -> list[dict]:
    """Найти монеты с всплеском объёма"""
    spikes = []
    now = time.time()

    for t in tickers:
        symbol = t["symbol"]
        vol    = t["volume_24h"]

        # Сохраняем историю
        volume_history[symbol].append(vol)
        if len(volume_history[symbol]) > HISTORY_PERIODS:
            volume_history[symbol].pop(0)

        # Нужно минимум 3 периода для расчёта среднего
        if len(volume_history[symbol]) < 3:
            continue

        # Среднее без последнего значения
        history = volume_history[symbol][:-1]
        avg_vol = sum(history) / len(history)

        if avg_vol <= 0:
            continue

        ratio = vol / avg_vol

        # Проверяем условия
        if vol >= MIN_VOLUME_USD and ratio >= SPIKE_MULTIPLIER:
            # Антиспам — не отправляем повторно
            last_alert = alerted_coins.get(symbol, 0)
            if now - last_alert < ALERT_COOLDOWN:
                continue

            alerted_coins[symbol] = now
            spikes.append({
                **t,
                "avg_vol": avg_vol,
                "ratio":   ratio,
            })

    return spikes


def send_spike_alert(spike: dict):
    """Отправить сигнал о всплеске"""
    symbol     = spike["symbol"].replace("USDT", "")
    price      = spike["price"]
    vol        = spike["volume_24h"]
    avg_vol    = spike["avg_vol"]
    ratio      = spike["ratio"]
    change_pct = spike["change_pct"]
    oi_value   = spike["oi_value"]

    direction = "РОСТ" if change_pct >= 0 else "ПАДЕНИЕ"
    change_sign = "+" if change_pct >= 0 else ""

    msg = (
        f"<b>ВСПЛЕСК ОБЪЕМА - {symbol}</b>\n"
        f"{'='*30}\n"
        f"Цена:        <b>{price:.6g} USDT</b>\n"
        f"Изменение:   <b>{change_sign}{change_pct:.1f}%</b>  ({direction})\n"
        f"\n"
        f"Объем 24ч:   <b>{fmt_volume(vol)}</b>\n"
        f"Средний:     {fmt_volume(avg_vol)}\n"
        f"Всплеск:     <b>x{ratio:.1f}</b>\n"
        f"ОИ (стоим.): {fmt_volume(oi_value)}\n"
        f"\n"
        f"Bybit: https://www.bybit.com/trade/usdt/{symbol}USDT\n"
        f"\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram(msg)
    log.info(f"SPIKE: {symbol} | vol={fmt_volume(vol)} | x{ratio:.1f}")


def send_top_report(tickers: list[dict]):
    """Отправить топ монет по объёму"""
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
        lines.append(
            f"{i:2}. <b>{symbol:<12}</b> {vol:<10} {arrow}{sign}{chg:.1f}%"
        )

    send_telegram("\n".join(lines))


def main():
    log.info("Bybit Volume Bot запущен")
    log.info(f"Мин. объём: {fmt_volume(MIN_VOLUME_USD)}")
    log.info(f"Порог всплеска: x{SPIKE_MULTIPLIER}")
    log.info(f"Интервал проверки: {CHECK_INTERVAL}с")

    send_telegram(
        f"<b>Bybit Volume Bot запущен</b>\n"
        f"Мин. объём: {fmt_volume(MIN_VOLUME_USD)}\n"
        f"Порог всплеска: x{SPIKE_MULTIPLIER}\n"
        f"Проверка каждые {CHECK_INTERVAL} секунд\n\n"
        f"Жду данные для калибровки (~{HISTORY_PERIODS} мин)..."
    )

    iteration     = 0
    REPORT_EVERY  = 60  # присылать топ каждые N итераций

    while True:
        try:
            raw_tickers = get_bybit_tickers()
            if not raw_tickers:
                log.warning("Пустой ответ от Bybit, жду...")
                time.sleep(CHECK_INTERVAL)
                continue

            # Парсим тикеры
            tickers = [p for t in raw_tickers if (p := parse_ticker(t))]
            log.info(f"Получено {len(tickers)} монет")

            # Ищем всплески
            spikes = check_spikes(tickers)
            for spike in spikes:
                send_spike_alert(spike)

            # Каждые REPORT_EVERY итераций — топ отчёт
            iteration += 1
            if iteration % REPORT_EVERY == 0:
                send_top_report(tickers)
                log.info("Отправлен топ отчёт")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
            send_telegram("Бот остановлен")
            break
        except Exception as e:
            log.error(f"Ошибка: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
