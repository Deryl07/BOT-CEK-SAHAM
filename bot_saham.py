import os
import math
import time
import hashlib
import sqlite3
import logging
import threading
from datetime import datetime

import pytz
import requests
import feedparser
import numpy as np
import pandas as pd
import yfinance as yf
from curl_cffi import requests as cffi_requests
from flask import Flask, request, jsonify
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

# ==============================================================================
# KONFIGURASI & ENVIRONMENT VARIABLES
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BotSahamBEI")

app = Flask(__name__)
WIB = pytz.timezone("Asia/Jakarta")

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
DISCORD_APP_ID = os.getenv("DISCORD_APP_ID", "").strip()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
CRON_SECRET = os.getenv("CRON_SECRET", "ganti-rahasia-ini").strip()
GOAPI_KEY = os.getenv("GOAPI_KEY", "").strip()  # Opsional (tier gratis GOAPI.IO)
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "25"))  # Default tiap 25 detik

DEFAULT_WATCHLIST = [
    s.strip().upper()
    for s in os.getenv("DEFAULT_WATCHLIST", "BBCA,BBRI,BMRI,TLKM,ASII,ADRO").split(",")
    if s.strip()
]

DB_PATH = os.getenv("DB_PATH", "/tmp/bot_saham_bei.db")
ROUND_TRIP_FEE = 0.0040  # ~0.15% beli + ~0.25% jual

BEI_HOLIDAYS = {
    "2026-01-01", "2026-01-16", "2026-02-16", "2026-02-17", "2026-03-18",
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24", "2026-04-03",
    "2026-05-01", "2026-05-14", "2026-05-15", "2026-05-27", "2026-05-28",
    "2026-06-01", "2026-06-16", "2026-08-17", "2026-08-25", "2026-12-24",
    "2026-12-25", "2026-12-31",
}

# ==============================================================================
# DATABASE SQLITE (WAL MODE) & SEEDING OTOMATIS
# ==============================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            source TEXT DEFAULT 'MANUAL',
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS portfolio (
            ticker TEXT PRIMARY KEY,
            entry_price REAL,
            lots INTEGER,
            highest_high REAL,
            locked_stop REAL,
            target_tp REAL,
            opened_at TEXT
        );
        CREATE TABLE IF NOT EXISTS signal_states (
            ticker TEXT PRIMARY KEY,
            last_state TEXT,
            last_alert_ts REAL
        );
        CREATE TABLE IF NOT EXISTS signals_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            signal_type TEXT,
            signal_price REAL,
            stop_price REAL,
            target_price REAL,
            score REAL,
            regime TEXT,
            created_at TEXT,
            eval_price REAL,
            net_return_pct REAL,
            status TEXT DEFAULT 'OPEN'
        );
        CREATE TABLE IF NOT EXISTS news_seen (
            url_hash TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT
        );
        """)
        cur = conn.execute("SELECT COUNT(*) AS c FROM watchlist").fetchone()
        if cur["c"] == 0:
            now_str = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
            for t in DEFAULT_WATCHLIST:
                conn.execute(
                    "INSERT OR IGNORE INTO watchlist (ticker, source, added_at) VALUES (?, 'SEED', ?)",
                    (t, now_str),
                )


init_db()

# ==============================================================================
# REGULASI MIKROSTRUKTUR BEI: FRAKSI HARGA, ARA/ARB ASIMETRIS & JAM BURSA
# ==============================================================================
def get_tick_size(price: float) -> int:
    if price < 200:
        return 1
    elif price < 500:
        return 2
    elif price < 2000:
        return 5
    elif price < 5000:
        return 10
    return 25


def round_to_tick(price: float, mode: str = "nearest") -> int:
    price = max(1.0, float(price))
    tick = get_tick_size(price)
    if mode == "down":
        return int(math.floor(price / tick) * tick)
    elif mode == "up":
        return int(math.ceil(price / tick) * tick)
    return int(round(price / tick) * tick)


def calc_ara_arb_bei(prev_close: float, is_fca: bool = False):
    if prev_close <= 0:
        return 0, 0
    if is_fca:
        if prev_close <= 10:
            return int(prev_close + 1), max(1, int(prev_close - 1))
        ara = round_to_tick(prev_close * 1.10, "down")
        arb = max(1, round_to_tick(prev_close * 0.90, "up"))
        return ara, arb

    if prev_close <= 10:
        ara = int(prev_close + 1)
    elif prev_close <= 200:
        ara = round_to_tick(prev_close * 1.35, "down")
    elif prev_close <= 5000:
        ara = round_to_tick(prev_close * 1.25, "down")
    else:
        ara = round_to_tick(prev_close * 1.20, "down")

    if prev_close <= 14:
        arb = max(1, int(prev_close - 1))
    else:
        arb = max(50, round_to_tick(prev_close * 0.93, "up"))
    return ara, arb


def get_market_session_status(now_wib: datetime = None) -> str:
    now = now_wib or datetime.now(WIB)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in BEI_HOLIDAYS:
        return "CLOSED_HOLIDAY"
    hhmm = now.hour * 100 + now.minute
    is_friday = now.weekday() == 4
    if 845 <= hhmm < 900:
        return "PRE_OPEN"
    if is_friday:
        if 900 <= hhmm <= 1130:
            return "SESSION_1"
        if 1130 < hhmm < 1400:
            return "LUNCH_BREAK"
        if 1400 <= hhmm <= 1549:
            return "SESSION_2"
    else:
        if 900 <= hhmm <= 1200:
            return "SESSION_1"
        if 1200 < hhmm < 1330:
            return "LUNCH_BREAK"
        if 1330 <= hhmm <= 1549:
            return "SESSION_2"
    if 1550 <= hhmm <= 1615:
        return "PRE_CLOSE"
    return "AFTER_HOURS"


# ==============================================================================
# LAYER DATA HIBRIDA & BATCH INTRADAY ANTI-BLOKIR
# ==============================================================================
_CACHE = {"idx_summary": (0, {}), "fca_set": (0, set()), "regime": (0, None), "hist": {}}


def fetch_idx_stock_summary() -> dict:
    now = time.time()
    if now - _CACHE["idx_summary"][0] < 600 and _CACHE["idx_summary"][1]:
        return _CACHE["idx_summary"][1]
    url = "https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0"
    try:
        resp = cffi_requests.get(url, impersonate="chrome120", timeout=12)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            mapping = {str(r.get("StockCode", "")).strip().upper(): r for r in data if r.get("StockCode")}
            if mapping:
                _CACHE["idx_summary"] = (now, mapping)
                return mapping
    except Exception as e:
        logger.warning(f"Gagal tarik IDX GetStockSummary: {e}")
    return _CACHE["idx_summary"][1]


def fetch_fca_watchlist() -> set:
    now = time.time()
    if now - _CACHE["fca_set"][0] < 3600 and _CACHE["fca_set"][1]:
        return _CACHE["fca_set"][1]
    url = "https://www.idx.co.id/primary/ListedCompany/GetSpecialMonitoring?indexFrom=0&pageSize=500"
    try:
        resp = cffi_requests.get(url, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("Results", [])
            fca_codes = {str(x.get("KodeEmiten", "")).strip().upper() for x in items if x.get("KodeEmiten")}
            _CACHE["fca_set"] = (now, fca_codes)
            return fca_codes
    except Exception as e:
        logger.warning(f"Gagal tarik daftar FCA BEI: {e}")
    return _CACHE["fca_set"][1]


def fetch_goapi_batch(tickers: list) -> dict:
    """Tarik harga real-time banyak saham sekaligus via GOAPI.IO jika GOAPI_KEY diisi."""
    if not GOAPI_KEY or not tickers:
        return {}
    try:
        symbols = ",".join(tickers[:20])
        r = requests.get(
            f"https://api.goapi.io/stock/idx/prices?symbols={symbols}",
            headers={"X-API-KEY": GOAPI_KEY},
            timeout=6,
        )
        if r.status_code == 200:
            results = r.json().get("data", {}).get("results", [])
            return {str(x.get("symbol", "")).upper(): x for x in results if x.get("symbol")}
    except Exception:
        pass
    return {}


def fetch_daily_history(ticker_yf: str, period: str = "5y") -> pd.DataFrame:
    """Cache data harian 20 menit agar loop 25 detik tidak membakar kuota."""
    now = time.time()
    cache_key = f"{ticker_yf}_{period}"
    if cache_key in _CACHE["hist"] and now - _CACHE["hist"][cache_key][0] < 1200:
        return _CACHE["hist"][cache_key][1].copy()
    try:
        df = yf.download(ticker_yf, period=period, interval="1d", progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        if not df.empty:
            _CACHE["hist"][cache_key] = (now, df)
        return df.copy()
    except Exception as e:
        logger.warning(f"Gagal tarik historis {ticker_yf}: {e}")
        return pd.DataFrame()


def fetch_batch_intraday_1m(tickers: list) -> dict:
    """Tarik data 1-menitan seluruh saham dalam 1x HTTP request."""
    if not tickers:
        return {}
    yf_syms = [f"{t}.JK" for t in tickers]
    try:
        df = yf.download(yf_syms, period="1d", interval="1m", progress=False, auto_adjust=False)
        if df.empty:
            return {}
        out = {}
        if isinstance(df.columns, pd.MultiIndex):
            for t in tickers:
                sym = f"{t}.JK"
                if sym in df.columns.get_level_values(1):
                    sub = df.xs(sym, axis=1, level=1).dropna(subset=["Close"])
                    if not sub.empty:
                        out[t] = sub
        else:
            sub = df.dropna(subset=["Close"])
            if not sub.empty and len(tickers) == 1:
                out[tickers[0]] = sub
        return out
    except Exception as e:
        logger.warning(f"Batch intraday error: {e}")
        return {}


# ==============================================================================
# OTAK KUANTITATIF: INDIKATOR EMPIRIS & REZIM PASAR
# ==============================================================================
def calc_wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift(1)).abs()
    low_close = (df["Low"] - df["Close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calc_cmf(df: pd.DataFrame, period: int = 20) -> float:
    hl_range = (df["High"] - df["Low"]).replace(0, np.nan)
    mf_multiplier = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / hl_range
    mf_volume = mf_multiplier.fillna(0) * df["Volume"]
    vol_sum = df["Volume"].rolling(period).sum().iloc[-1]
    if not vol_sum or np.isnan(vol_sum):
        return 0.0
    return float(mf_volume.rolling(period).sum().iloc[-1] / vol_sum)


def wilson_lower_bound(wins: int, n: int, z: float = 1.645) -> float:
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    rad = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (center - rad) / denom


def get_market_regime() -> dict:
    now = time.time()
    if _CACHE["regime"][1] and now - _CACHE["regime"][0] < 900:
        return _CACHE["regime"][1]

    ihsg = fetch_daily_history("^JKSE", period="6mo")
    usdidr = fetch_daily_history("IDR=X", period="3mo")

    regime_label = "NETRAL"
    min_buy_score = 72
    ihsg_close = 0.0
    if len(ihsg) >= 50:
        close = ihsg["Close"]
        ihsg_close = float(close.iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        if ihsg_close > ema20 and ema20 > ema50:
            regime_label = "BULLISH (RISK-ON)"
            min_buy_score = 68
        elif ihsg_close < ema50:
            regime_label = "BEARISH (DEFENSIF)"
            min_buy_score = 82

    idr_note = "Stabil"
    if len(usdidr) >= 20:
        idr_now = float(usdidr["Close"].iloc[-1])
        idr_ema20 = float(usdidr["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        if idr_now > idr_ema20 * 1.006:
            idr_note = f"Rupiah Melemah ({idr_now:,.0f})"
            min_buy_score += 3
        elif idr_now < idr_ema20 * 0.994:
            idr_note = f"Rupiah Menguat ({idr_now:,.0f})"

    with get_db() as conn:
        rows = conn.execute(
            "SELECT net_return_pct FROM signals_ledger WHERE status='CLOSED' ORDER BY id DESC LIMIT 20"
        ).fetchall()
    if len(rows) >= 8:
        avg_exp = np.mean([r["net_return_pct"] for r in rows])
        if avg_exp < 0:
            min_buy_score = max(min_buy_score, 85)
            regime_label += " + THROTTLE AKTIF"

    res = {
        "regime": regime_label,
        "min_buy_score": min_buy_score,
        "ihsg_close": ihsg_close,
        "ihsg_series": ihsg["Close"] if not ihsg.empty else pd.Series(dtype=float),
        "idr_note": idr_note,
    }
    _CACHE["regime"] = (now, res)
    return res


def analyze_stock(
    ticker: str,
    idx_summary: dict = None,
    fca_set: set = None,
    regime: dict = None,
    intraday_df: pd.DataFrame = None,
    goapi_row: dict = None,
) -> dict:
    ticker = ticker.upper().replace(".JK", "").strip()
    idx_summary = idx_summary if idx_summary is not None else fetch_idx_stock_summary()
    fca_set = fca_set if fca_set is not None else fetch_fca_watchlist()
    regime = regime if regime is not None else get_market_regime()

    is_fca = ticker in fca_set
    df = fetch_daily_history(f"{ticker}.JK", period="5y")
    if df.empty or len(df) < 55:
        return {"ticker": ticker, "error": "Data historis tidak cukup atau kode salah."}

    idx_row = idx_summary.get(ticker, {})
    last_price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    foreign_net_val = 0.0

    if idx_row:
        if float(idx_row.get("Close", 0)) > 0:
            last_price = float(idx_row["Close"])
        if float(idx_row.get("Previous", 0)) > 0:
            prev_close = float(idx_row["Previous"])
        foreign_net_val = float(idx_row.get("ForeignBuy", 0)) - float(idx_row.get("ForeignSell", 0))

    # Timpa dengan harga intraday terbaru (Batch 1m -> GOAPI)
    vwap_today = last_price
    rvol = 1.0
    above_vwap = True
    if intraday_df is not None and not intraday_df.empty:
        last_price = float(intraday_df["Close"].iloc[-1])
        tp = (intraday_df["High"] + intraday_df["Low"] + intraday_df["Close"]) / 3
        vol_sum = intraday_df["Volume"].sum()
        if vol_sum > 0:
            vwap_today = float((tp * intraday_df["Volume"]).sum() / vol_sum)
        above_vwap = last_price >= vwap_today
        avg_daily_vol = float(df["Volume"].tail(20).mean())
        if avg_daily_vol > 0:
            # Estimasi proporsi waktu bursa (330 menit/hari)
            frac_day = max(0.05, min(1.0, len(intraday_df) / 330.0))
            rvol = float(vol_sum / (avg_daily_vol * frac_day))

    if goapi_row and float(goapi_row.get("close", 0)) > 0:
        last_price = float(goapi_row["close"])

    ara_price, arb_price = calc_ara_arb_bei(prev_close, is_fca=is_fca)
    locked_arb = (arb_price > 0) and (last_price <= arb_price)

    close = df["Close"]
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    rsi14 = float(calc_wilder_rsi(close, 14).iloc[-1])

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_hist = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False).mean()).iloc[-1])

    atr14 = float(calc_atr(df, 14).iloc[-1])
    atr22 = float(calc_atr(df, 22).iloc[-1])
    highest_22 = float(df["High"].tail(22).max())

    adtv_20d = float((df["Close"] * df["Volume"]).tail(20).mean())

    rs_outperform = False
    ihsg_s = regime.get("ihsg_series", pd.Series(dtype=float))
    if not ihsg_s.empty:
        aligned = pd.DataFrame({"s": close, "m": ihsg_s}).dropna()
        if len(aligned) >= 20:
            rs_ratio = aligned["s"] / aligned["m"]
            rs_outperform = bool(rs_ratio.iloc[-1] > rs_ratio.rolling(20).mean().iloc[-1])

    obv = (np.sign(close.diff()).fillna(0) * df["Volume"]).cumsum()
    obv_rising = bool(obv.iloc[-1] > obv.rolling(10).mean().iloc[-1])
    bull_div = bool((close.iloc[-1] < close.iloc[-10]) and (obv.iloc[-1] > obv.iloc[-10]))
    bear_div = bool((close.iloc[-1] > close.iloc[-10]) and (obv.iloc[-1] < obv.iloc[-10]))
    cmf20 = calc_cmf(df, 20)

    m_now = datetime.now(WIB).month
    df_hist = df.copy()
    df_hist["year"] = df_hist.index.year
    df_hist["month"] = df_hist.index.month
    monthly_returns = []
    for yr in sorted(df_hist["year"].unique())[:-1]:
        sub_m = df_hist[(df_hist["year"] == yr) & (df_hist["month"] == m_now)]
        if len(sub_m) >= 5:
            monthly_returns.append(((sub_m["Close"].iloc[-1] - sub_m["Open"].iloc[0]) / sub_m["Open"].iloc[0]) * 100)
    wins = sum(1 for r in monthly_returns if r > 0)
    win_rate_pct = (wins / len(monthly_returns) * 100) if monthly_returns else 50.0
    wilson_score = wilson_lower_bound(wins, len(monthly_returns)) * 100
    median_m_ret = float(np.median(monthly_returns)) if monthly_returns else 3.0

    # Skor Komposit (0 - 100)
    score = 0.0
    reasons = []
    if last_price > ema20 > ema50:
        score += 18
        reasons.append("Uptrend kuat (Harga > EMA20 > EMA50)")
    elif last_price > ema20:
        score += 11
        reasons.append("Harga di atas EMA20")
    if rs_outperform:
        score += 7
        reasons.append("Outperform IHSG (RS naik)")

    if above_vwap:
        score += 8
        reasons.append(f"Bertahan di atas VWAP ({vwap_today:,.0f})")
    if 1.4 <= rvol <= 2.5:
        score += 10
        reasons.append(f"RVOL ideal ({rvol:.2f}x)")
    elif rvol > 2.5:
        score += 5
        reasons.append(f"Lonjakan RVOL ekstrem ({rvol:.2f}x)")
    if bull_div or (obv_rising and cmf20 > 0.05):
        score += 12
        reasons.append(f"Akumulasi OBV & CMF positif ({cmf20:+.2f})")
    elif obv_rising:
        score += 6

    if foreign_net_val > 1_000_000_000:
        score += 20
        reasons.append(f"Net Foreign Buy (+Rp{foreign_net_val/1e9:.2f} M)")
    elif foreign_net_val > 0:
        score += 12
        reasons.append("Net Foreign Buy tipis")
    elif foreign_net_val == 0:
        score += 8

    if 50 <= rsi14 <= 68:
        score += 9
        reasons.append(f"Wilder RSI sehat ({rsi14:.1f})")
    elif 42 <= rsi14 < 50 and bull_div:
        score += 6
    if macd_hist > 0:
        score += 6
        reasons.append("Histogram MACD positif")

    if wilson_score >= 45:
        score += 10
    elif wilson_score >= 30:
        score += 5

    # Manajemen Risiko
    entry_ideal = round_to_tick(min(last_price, max(ema20, vwap_today)), "nearest")
    raw_stop = entry_ideal - (1.5 * atr14)
    stop_loss = max(50 if not is_fca else 1, round_to_tick(raw_stop, "down"))
    if stop_loss >= entry_ideal:
        stop_loss = max(1, entry_ideal - get_tick_size(entry_ideal))

    chandelier_stop = max(50 if not is_fca else 1, round_to_tick(highest_22 - (2.5 * atr22), "down"))
    risk_per_share = max(get_tick_size(entry_ideal), entry_ideal - stop_loss)
    target_tp = round_to_tick(max(entry_ideal + (2.2 * risk_per_share), entry_ideal * (1 + max(0.03, median_m_ret / 100))), "up")
    if ara_price > entry_ideal:
        target_tp = min(target_tp, ara_price)

    net_reward_pct = ((target_tp - entry_ideal) / entry_ideal - ROUND_TRIP_FEE) * 100
    net_risk_pct = ((entry_ideal - stop_loss) / entry_ideal + ROUND_TRIP_FEE) * 100
    rr_ratio = (net_reward_pct / net_risk_pct) if net_risk_pct > 0 else 0.0

    warnings = []
    if is_fca:
        warnings.append("PAPAN PEMANTAUAN KHUSUS (FCA)")
    if locked_arb:
        warnings.append(f"TERKUNCI ARB (Rp{arb_price:,})")
    if adtv_20d < 3_000_000_000:
        warnings.append(f"Likuiditas rendah (ADTV Rp{adtv_20d/1e9:.2f} M)")
    if bear_div and cmf20 < -0.05:
        warnings.append("Bearish Divergence OBV + Distribusi CMF")

    with get_db() as conn:
        pos = conn.execute("SELECT * FROM portfolio WHERE ticker=?", (ticker,)).fetchone()

    active_stop = stop_loss
    if pos:
        new_hh = max(float(pos["highest_high"]), float(df["High"].iloc[-1]), last_price)
        new_cand = round_to_tick(new_hh - (2.5 * atr22), "down")
        active_stop = max(int(pos["locked_stop"]), new_cand)
        with get_db() as conn:
            conn.execute(
                "UPDATE portfolio SET highest_high=?, locked_stop=? WHERE ticker=?",
                (new_hh, active_stop, ticker),
            )

    min_score_req = regime["min_buy_score"]
    if is_fca or locked_arb:
        action = "HINDARI / WASPADA"
    elif pos and last_price <= active_stop:
        action = "CUT-LOSS / TRAILING STOP"
    elif (last_price < ema20 and not above_vwap and bear_div) or (rsi14 < 38 and cmf20 < -0.08):
        action = "JUAL / DISTRIBUSI"
    elif score >= min_score_req and rr_ratio >= 2.0 and adtv_20d >= 3_000_000_000:
        action = "BELI"
    else:
        action = "PANTAU"

    return {
        "ticker": ticker,
        "action": action,
        "score": round(score, 1),
        "min_score_req": min_score_req,
        "last_price": int(last_price),
        "prev_close": int(prev_close),
        "chg_pct": ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0,
        "entry_ideal": entry_ideal,
        "target_tp": target_tp,
        "stop_loss": active_stop,
        "rr_ratio": round(rr_ratio, 2),
        "rsi14": round(rsi14, 1),
        "rvol": round(rvol, 2),
        "vwap": int(vwap_today),
        "cmf20": round(cmf20, 2),
        "foreign_net_m": round(foreign_net_val / 1e9, 2),
        "adtv_m": round(adtv_20d / 1e9, 2),
        "win_rate_pct": round(win_rate_pct, 0),
        "wilson_pct": round(wilson_score, 0),
        "ara": ara_price,
        "arb": arb_price,
        "is_fca": is_fca,
        "reasons": reasons[:3],
        "warnings": warnings,
        "in_portfolio": bool(pos),
    }


# ==============================================================================
# SCREENER OTOMATIS & PEMANGKASAN WATCHLIST
# ==============================================================================
def run_eod_screener_and_pruner():
    idx_summary = fetch_idx_stock_summary()
    fca_set = fetch_fca_watchlist()
    regime = get_market_regime()

    candidates = []
    for code, row in idx_summary.items():
        if len(code) != 4 or code in fca_set:
            continue
        val = float(row.get("Value", 0))
        close_p = float(row.get("Close", 0))
        prev_p = float(row.get("Previous", 0))
        f_net = float(row.get("ForeignBuy", 0)) - float(row.get("ForeignSell", 0))
        if val >= 15_000_000_000 and close_p >= 100 and prev_p > 0:
            chg = (close_p - prev_p) / prev_p
            if 0.005 <= chg <= 0.15:
                candidates.append((f_net + val * 0.05, code))

    candidates.sort(reverse=True)
    top_picks = []
    for _, code in candidates[:20]:
        res = analyze_stock(code, idx_summary, fca_set, regime)
        if not res.get("error") and res["score"] >= regime["min_buy_score"] and res["rr_ratio"] >= 2.0:
            top_picks.append(res)
        if len(top_picks) >= 5:
            break

    pruned = []
    with get_db() as conn:
        wl_rows = conn.execute("SELECT ticker FROM watchlist").fetchall()
        port_tickers = {r["ticker"] for r in conn.execute("SELECT ticker FROM portfolio").fetchall()}

    for r in wl_rows:
        t = r["ticker"]
        if t in port_tickers:
            continue
        res = analyze_stock(t, idx_summary, fca_set, regime)
        if not res.get("error"):
            if res["is_fca"] or res["adtv_m"] < 1.5 or (res["score"] < 30 and res["cmf20"] < -0.08):
                with get_db() as conn:
                    conn.execute("DELETE FROM watchlist WHERE ticker=?", (t,))
                pruned.append(f"{t} (Skor {res['score']}, ADTV Rp{res['adtv_m']}M)")

    with get_db() as conn:
        open_sigs = conn.execute("SELECT * FROM signals_ledger WHERE status='OPEN'").fetchall()
        for s in open_sigs:
            t = s["ticker"]
            if t in idx_summary and float(idx_summary[t].get("Close", 0)) > 0:
                curr_p = float(idx_summary[t]["Close"])
                entry_p = float(s["signal_price"])
                if curr_p >= float(s["target_price"]) or curr_p <= float(s["stop_price"]):
                    net_ret = ((curr_p - entry_p) / entry_p - ROUND_TRIP_FEE) * 100
                    conn.execute(
                        "UPDATE signals_ledger SET eval_price=?, net_return_pct=?, status='CLOSED' WHERE id=?",
                        (curr_p, round(net_ret, 2), s["id"]),
                    )

    return top_picks, pruned, regime


# ==============================================================================
# PENGUMPUL BERITA TEPERCAYA
# ==============================================================================
def fetch_trusted_news(filter_tickers: set = None) -> list:
    items = []
    try:
        url = "https://www.idx.co.id/primary/NewsAnnouncement/GetAllAnnouncement?indexFrom=0&pageSize=30"
        resp = cffi_requests.get(url, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            for r in resp.json().get("Replies", []):
                kode = str(r.get("Kode_Emiten", "")).strip().upper()
                judul = str(r.get("JudulPengumuman", "")).strip()
                link = f"https://www.idx.co.id{r.get('Attachments', [{}])[0].get('FullSavePath', '')}" if r.get("Attachments") else "https://www.idx.co.id"
                if filter_tickers and kode not in filter_tickers:
                    continue
                if any(k in judul.lower() for k in ["bulanan registrasi", "laporan kepemilikan"]):
                    continue
                items.append({"source": "IDX Resmi", "ticker": kode, "title": judul, "link": link})
    except Exception:
        pass

    for src_name, feed_url in [("CNBC Indonesia", "https://www.cnbcindonesia.com/market/rss"), ("Kontan", "https://www.kontan.co.id/feed")]:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                matched_t = ""
                if filter_tickers:
                    for t in filter_tickers:
                        if f"({t})" in title.upper() or f" {t} " in f" {title.upper()} ":
                            matched_t = t
                            break
                    if not matched_t:
                        continue
                items.append({"source": src_name, "ticker": matched_t or "MARKET", "title": title, "link": link})
        except Exception:
            pass
    return items[:10]


# ==============================================================================
# DISCORD EMBEDS & FOLLOW-UP SENDER
# ==============================================================================
def build_stock_embeds(results: list, regime: dict, title_prefix: str = "Analisis Saham BEI") -> list:
    embeds = [{
        "title": f"📊 {title_prefix} ({datetime.now(WIB).strftime('%d %b %Y %H:%M:%S WIB')})",
        "description": (
            f"**Rezim Pasar:** `{regime['regime']}` | **IHSG:** `{regime['ihsg_close']:,.2f}`\n"
            f"**Makro:** {regime['idr_note']} | **Syarat Skor Beli:** `>= {regime['min_buy_score']}/100`"
        ),
        "color": 0x2B2D31,
    }]

    color_map = {
        "BELI": 0x2ECC71,
        "PANTAU": 0xF1C40F,
        "JUAL / DISTRIBUSI": 0xE67E22,
        "CUT-LOSS / TRAILING STOP": 0xE74C3C,
        "HINDARI / WASPADA": 0x992D22,
    }

    for r in results[:9]:
        if r.get("error"):
            embeds.append({"title": f"❌ {r['ticker']}", "description": r["error"], "color": 0x95A5A6})
            continue
        act = r["action"]
        tag_port = " 💼 [PORTOFOLIO]" if r["in_portfolio"] else ""
        desc_lines = [
            f"**Sinyal:** `{act}` (Skor: **{r['score']}**/{r['min_score_req']}){tag_port}",
            f"**Harga:** `Rp{r['last_price']:,}` ({r['chg_pct']:+.2f}%) | **VWAP:** `Rp{r['vwap']:,}`",
            f"**Plan:** Entry `Rp{r['entry_ideal']:,}` | TP `Rp{r['target_tp']:,}` | SL/Trail `Rp{r['stop_loss']:,}` (R:R `{r['rr_ratio']}x`)",
            f"**Indikator:** RSI `{r['rsi14']}` | RVOL `{r['rvol']}x` | CMF `{r['cmf20']:+.2f}` | Asing `Rp{r['foreign_net_m']:+.2f}M`",
            f"**Seasonality:** Win `{r['win_rate_pct']:.0f}%` (Wilson `{r['wilson_pct']:.0f}%`) | ARA `{r['ara']:,}` / ARB `{r['arb']:,}`",
        ]
        if r["reasons"]:
            desc_lines.append("✅ " + " • ".join(r["reasons"]))
        if r["warnings"]:
            desc_lines.append("⚠️ " + " | ".join(r["warnings"]))
        embeds.append({"title": f"{r['ticker']} — {act}", "description": "\n".join(desc_lines), "color": color_map.get(act, 0x3498DB)})
    return embeds


def send_discord_embeds(embeds: list, interaction_token: str = None):
    payload = {"embeds": embeds[:10]}
    try:
        if interaction_token and DISCORD_APP_ID:
            url = f"https://discord.com/api/v10/webhooks/{DISCORD_APP_ID}/{interaction_token}"
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code in (200, 204):
                return
        if DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Gagal kirim Discord embed: {e}")


# ==============================================================================
# SIKLUS PEMINDAIAN INTI & BACKGROUND LOOP TIAP 25 DETIK
# ==============================================================================
def execute_realtime_scan(check_news: bool = True) -> dict:
    with get_db() as conn:
        wl = [r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist").fetchall()]
        pt = [r["ticker"] for r in conn.execute("SELECT ticker FROM portfolio").fetchall()]
    tickers = list(dict.fromkeys(pt + wl))

    idx_sum = fetch_idx_stock_summary()
    fca_set = fetch_fca_watchlist()
    regime = get_market_regime()
    intraday_map = fetch_batch_intraday_1m(tickers)
    goapi_map = fetch_goapi_batch(tickers)

    alerts = []
    now_ts = time.time()
    for t in tickers:
        res = analyze_stock(t, idx_sum, fca_set, regime, intraday_map.get(t), goapi_map.get(t))
        if res.get("error"):
            continue
        act = res["action"]
        if act in ("BELI", "CUT-LOSS / TRAILING STOP", "JUAL / DISTRIBUSI"):
            with get_db() as conn:
                st = conn.execute("SELECT * FROM signal_states WHERE ticker=?", (t,)).fetchone()
                if st and st["last_state"] == act and (now_ts - float(st["last_alert_ts"])) < 2700:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO signal_states (ticker, last_state, last_alert_ts) VALUES (?, ?, ?)",
                    (t, act, now_ts),
                )
                if act == "BELI":
                    conn.execute(
                        """INSERT INTO signals_ledger (ticker, signal_type, signal_price, stop_price, target_price, score, regime, created_at)
                           VALUES (?, 'BELI', ?, ?, ?, ?, ?, ?)""",
                        (t, res["entry_ideal"], res["stop_loss"], res["target_tp"], res["score"], regime["regime"], datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")),
                    )
            alerts.append(res)

    if alerts:
        send_discord_embeds(build_stock_embeds(alerts, regime, "🚨 Sinyal Near Real-Time BEI"))

    fresh_news = []
    if check_news:
        news_items = fetch_trusted_news(set(tickers))
        with get_db() as conn:
            for n in news_items:
                h = hashlib.sha256((n["title"] + n["source"]).encode()).hexdigest()[:24]
                if not conn.execute("SELECT 1 FROM news_seen WHERE url_hash=?", (h,)).fetchone():
                    conn.execute(
                        "INSERT INTO news_seen (url_hash, title, created_at) VALUES (?, ?, ?)",
                        (h, n["title"], datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    fresh_news.append(n)
        if fresh_news:
            desc = "\n".join(f"• **[{n['source']} - {n['ticker']}]** [{n['title']}]({n['link']})" for n in fresh_news[:5])
            send_discord_embeds([{"title": "📢 Berita & Keterbukaan Baru", "description": desc, "color": 0xE67E22}])

    return {"alerts_sent": len(alerts), "news_sent": len(fresh_news)}


def background_realtime_worker():
    """Loop otomatis setiap 25 detik selama jam bursa BEI aktif."""
    logger.info(f"Background scanner aktif (interval {SCAN_INTERVAL_SEC} detik)...")
    last_news_ts = 0
    while True:
        try:
            session = get_market_session_status()
            if session in ("SESSION_1", "SESSION_2", "PRE_CLOSE"):
                now_ts = time.time()
                do_news = (now_ts - last_news_ts) >= 180  # Cek berita tiap 3 menit
                if do_news:
                    last_news_ts = now_ts
                execute_realtime_scan(check_news=do_news)
                time.sleep(SCAN_INTERVAL_SEC)
            else:
                time.sleep(60)  # Tidur hemat resource di luar jam bursa
        except Exception as e:
            logger.error(f"Error pada background loop: {e}")
            time.sleep(30)


# Jalankan 1 thread pemindai latar belakang saat server Gunicorn menyala
threading.Thread(target=background_realtime_worker, daemon=True).start()


# ==============================================================================
# DISCORD SIGNATURE VERIFICATION & SLASH COMMANDS WORKER
# ==============================================================================
def verify_discord_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    if not DISCORD_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(timestamp.encode("utf-8") + raw_body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def process_slash_command(data: dict, interaction_token: str):
    cmd = data.get("name", "")
    options = {opt["name"]: opt["value"] for opt in data.get("options", [])}

    if cmd == "cek":
        kode_arg = options.get("kode", "").strip().upper()
        if kode_arg:
            tickers = [k.strip() for k in kode_arg.split(",") if k.strip()]
        else:
            with get_db() as conn:
                wl = [r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist").fetchall()]
                pt = [r["ticker"] for r in conn.execute("SELECT ticker FROM portfolio").fetchall()]
            tickers = list(dict.fromkeys(pt + wl))

        idx_sum = fetch_idx_stock_summary()
        fca_set = fetch_fca_watchlist()
        regime = get_market_regime()
        intraday_map = fetch_batch_intraday_1m(tickers[:9])
        goapi_map = fetch_goapi_batch(tickers[:9])
        results = [analyze_stock(t, idx_sum, fca_set, regime, intraday_map.get(t), goapi_map.get(t)) for t in tickers[:9]]
        send_discord_embeds(build_stock_embeds(results, regime, "Hasil Pemindaian Saham"), interaction_token)

    elif cmd == "screener":
        top_picks, pruned, regime = run_eod_screener_and_pruner()
        embeds = build_stock_embeds(top_picks, regime, "Top Kandidat Screener BEI") if top_picks else [{
            "title": "🔍 Hasil Screener BEI",
            "description": f"Rezim `{regime['regime']}`: Belum ada emiten lolos filter ketat.",
            "color": 0xF1C40F,
        }]
        if pruned:
            embeds.append({"title": "🧹 Watchlist Dipangkas Otomatis", "description": "\n".join(f"- {p}" for p in pruned), "color": 0x95A5A6})
        send_discord_embeds(embeds, interaction_token)

    elif cmd == "beli":
        t = options["kode"].strip().upper()
        harga = float(options["harga"])
        lot = int(options.get("lot", 1))
        res = analyze_stock(t)
        sl = res.get("stop_loss", round_to_tick(harga * 0.94, "down"))
        tp = res.get("target_tp", round_to_tick(harga * 1.12, "up"))
        now_s = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            conn.execute(
                """INSERT INTO portfolio (ticker, entry_price, lots, highest_high, locked_stop, target_tp, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                   entry_price=excluded.entry_price, lots=excluded.lots,
                   highest_high=excluded.highest_high, locked_stop=excluded.locked_stop,
                   target_tp=excluded.target_tp""",
                (t, harga, lot, harga, sl, tp, now_s),
            )
            conn.execute("INSERT OR IGNORE INTO watchlist (ticker, source, added_at) VALUES (?, 'PORTFOLIO', ?)", (t, now_s))
        send_discord_embeds([{
            "title": f"💼 Posisi Dicatat: {t}",
            "description": f"Entry: `Rp{harga:,.0f}` ({lot} lot)\nChandelier/ATR Stop Terkunci: `Rp{sl:,}` | Target TP: `Rp{tp:,}`",
            "color": 0x2ECC71,
        }], interaction_token)

    elif cmd == "jual":
        t = options["kode"].strip().upper()
        with get_db() as conn:
            conn.execute("DELETE FROM portfolio WHERE ticker=?", (t,))
        send_discord_embeds([{"title": f"✅ Posisi Ditutup: {t}", "description": f"Emiten `{t}` dihapus dari portofolio aktif.", "color": 0x3498DB}], interaction_token)

    elif cmd == "watchlist":
        aksi = options["aksi"]
        t = options.get("kode", "").strip().upper()
        with get_db() as conn:
            if aksi == "tambah" and t:
                conn.execute("INSERT OR IGNORE INTO watchlist (ticker, source, added_at) VALUES (?, 'MANUAL', ?)", (t, datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")))
                msg = f"Emiten `{t}` ditambahkan ke watchlist."
            elif aksi == "hapus" and t:
                conn.execute("DELETE FROM watchlist WHERE ticker=?", (t,))
                msg = f"Emiten `{t}` dihapus dari watchlist."
            else:
                rows = [r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist").fetchall()]
                msg = "Daftar Pantauan Aktif: " + (", ".join(f"`{x}`" for x in rows) if rows else "Kosong")
        send_discord_embeds([{"title": "📋 Manajemen Watchlist", "description": msg, "color": 0x3498DB}], interaction_token)

    elif cmd == "berita":
        kode_arg = options.get("kode", "").strip().upper()
        news_list = fetch_trusted_news({kode_arg} if kode_arg else None)
        desc = "\n".join(f"• **[{n['source']} - {n['ticker']}]** [{n['title']}]({n['link']})" for n in news_list) if news_list else "Belum ada berita material terbaru."
        send_discord_embeds([{"title": "📰 Keterbukaan BEI & Berita Pasar", "description": desc[:3800], "color": 0x1ABC9C}], interaction_token)

    elif cmd == "performa":
        with get_db() as conn:
            closed = conn.execute("SELECT net_return_pct FROM signals_ledger WHERE status='CLOSED'").fetchall()
            open_cnt = conn.execute("SELECT COUNT(*) AS c FROM signals_ledger WHERE status='OPEN'").fetchone()["c"]
        if not closed:
            desc = f"Belum ada sinyal tertutup di ledger. Sinyal aktif dipantau: `{open_cnt}`."
        else:
            rets = [r["net_return_pct"] for r in closed]
            win_rate = sum(1 for x in rets if x > 0) / len(rets) * 100
            desc = f"Total Sinyal Tertutup: `{len(rets)}` | Win Rate Net-of-Fee: `{win_rate:.1f}%` | Rata-rata Return: `{np.mean(rets):+.2f}%` | Aktif: `{open_cnt}`"
        send_discord_embeds([{"title": "📈 Audit Akurasi Signal Ledger", "description": desc, "color": 0x9B59B6}], interaction_token)


# ==============================================================================
# ROUTES FLASK
# ==============================================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "session": get_market_session_status(), "time_wib": datetime.now(WIB).isoformat()}), 200


@app.route("/interactions", methods=["POST"])
def interactions():
    sig = request.headers.get("X-Signature-Ed25519", "")
    ts = request.headers.get("X-Signature-Timestamp", "")
    raw_body = request.data

    if not verify_discord_signature(raw_body, sig, ts):
        return "Invalid signature", 401

    payload = request.json
    if payload.get("type") == 1:
        return jsonify({"type": 1})

    if payload.get("type") == 2:
        threading.Thread(target=process_slash_command, args=(payload.get("data", {}), payload.get("token")), daemon=True).start()
        return jsonify({"type": 5})

    return jsonify({"type": 1})


@app.route("/cron/eod", methods=["GET", "POST"])
def cron_eod():
    if request.args.get("key") != CRON_SECRET:
        return "Unauthorized", 401
    now_wib = datetime.now(WIB)
    if now_wib.weekday() >= 5 or now_wib.strftime("%Y-%m-%d") in BEI_HOLIDAYS:
        return jsonify({"status": "skipped_holiday"}), 200

    top_picks, pruned, regime = run_eod_screener_and_pruner()
    embeds = build_stock_embeds(top_picks, regime, "🌙 Rekap Penutupan & Screener EOD BEI") if top_picks else [{
        "title": "🌙 Rekap Penutupan EOD BEI",
        "description": f"Rezim `{regime['regime']}` — Belum ada kandidat lolos seleksi EOD hari ini.",
        "color": 0x34495E,
    }]
    if pruned:
        embeds.append({"title": "🧹 Watchlist Dibuang Otomatis", "description": "\n".join(f"- {p}" for p in pruned), "color": 0x95A5A6})
    send_discord_embeds(embeds)
    return jsonify({"status": "eod_done", "picks": len(top_picks), "pruned": len(pruned)}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
