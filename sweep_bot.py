"""
sweep_bot.py — Bot de alertas para el sistema v2 (SPY/QQQ/TSLA, riesgo 20%/trade)
==================================================================================
Reglas congeladas de la v2 validada (jul-2024/jul-2026, PF 1.32, E[R]+0.20):
  - Solo Setup A: liquidity sweep de PDH/PDL/ONH/ONL + contracción + reclaim de VWAP
  - Solo entradas 9:30-10:00 ET
  - Filtro de régimen por ATR + calendario macro
  - Vehículo: debit spread 1-2 DTE, deltas ~0.50/0.28
  - Riesgo: 20% del equity por trade, 2 pérdidas diarias = fin del día,
    3 seguidas = tamaño al 50%

MODO: alertas por Telegram + gestión de la posición abierta (parcial 1.5R,
stop a BE, objetivo, cierre EOD). La ejecución es manual en thinkorswim (paper).

Deploy en Render como Cron Job (ver README):  arranca 9:20 ET los días hábiles,
corre hasta que cierra el trade o hasta las 16:00 ET.

Variables de entorno requeridas:
  TELEGRAM_TOKEN   token del bot (via @BotFather)
  TELEGRAM_CHAT_ID tu chat id
  EQUITY           equity actual de la cuenta paper (ej: 9000)
"""
import os, time, json, csv, math
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

ET = ZoneInfo("US/Eastern")

# ----------------------- CONFIG -----------------------
TICKERS = ["SPY", "QQQ", "TSLA"]          # universo validado — no agregar sin validar
RISK_FRAC = 0.20                           # elegido por Sofia (menu Monte Carlo)
SWEEP_MIN_PCT = 0.0005
VOL_MULT = 1.5
LOOKAHEAD_BARS = 6
ENTRY_DEADLINE = "10:00"                   # ET, congelado por la validación
PARTIAL_AT_R, PARTIAL_FRAC, TARGET_R = 1.5, 0.5, 2.5
MAX_DAY_LOSSES = 2
ATR_LOW_PCT, ATR_HIGH_PCT = 20, 95
LONG_DELTA, SHORT_DELTA = 0.50, 0.28
RF_RATE = 0.045
STATE_FILE = "bot_state.json"              # rachas y pérdidas del día
LOG_FILE = "trades_paper.csv"

# Calendario macro: completar/actualizar a mano desde federalreserve.gov y bls.gov.
# El bot NO opera estos días antes de las 10:00 (nuestra ventana entera).
MACRO_DATES = { # --- FOMC (fuente: Federal Reserve, confirmadas) ---
    "2026-07-29",   # FOMC (decisión)
    "2026-09-16",   # FOMC (decisión, con dot plot)
    "2026-10-28",   # FOMC (decisión)
    "2026-12-09",   # FOMC (decisión)
    # --- CPI (8:30 ET, 2da semana; confirmá cada mes en bls.gov/schedule) ---
    "2026-08-12",   # CPI julio (estimado)
    "2026-09-11",   # CPI agosto (estimado)
    "2026-10-13",   # CPI septiembre (estimado)
    "2026-11-13",   # CPI octubre (estimado)
    "2026-12-10",   # CPI noviembre (estimado)
} # --- FOMC (fuente: Federal Reserve, confirmadas) ---
    "2026-07-29",   # FOMC (decisión)
    "2026-09-16",   # FOMC (decisión, con dot plot)
    "2026-10-28",   # FOMC (decisión)
    "2026-12-09",   # FOMC (decisión)
    # --- CPI (8:30 ET, 2da semana; confirmá cada mes en bls.gov/schedule) ---
    "2026-08-12",   # CPI julio (estimado)
    "2026-09-11",   # CPI agosto (estimado)
    "2026-10-13",   # CPI septiembre (estimado)
    "2026-11-13",   # CPI octubre (estimado)
    "2026-12-10",   # CPI noviembre (estimado)
} # --- FOMC (fuente: Federal Reserve, confirmadas) ---
    "2026-07-29",   # FOMC (decisión)
    "2026-09-16",   # FOMC (decisión, con dot plot)
    "2026-10-28",   # FOMC (decisión)
    "2026-12-09",   # FOMC (decisión)
    # --- CPI (8:30 ET, 2da semana; confirmá cada mes en bls.gov/schedule) ---
    "2026-08-12",   # CPI julio (estimado)
    "2026-09-11",   # CPI agosto (estimado)
    "2026-10-13",   # CPI septiembre (estimado)
    "2026-11-13",   # CPI octubre (estimado)
    "2026-12-10",   # CPI noviembre (estimado)
} # --- FOMC (fuente: Federal Reserve, confirmadas) ---
    "2026-07-29",   # FOMC (decisión)
    "2026-09-16",   # FOMC (decisión, con dot plot)
    "2026-10-28",   # FOMC (decisión)
    "2026-12-09",   # FOMC (decisión)
    # --- CPI (8:30 ET, 2da semana; confirmá cada mes en bls.gov/schedule) ---
    "2026-08-12",   # CPI julio (estimado)
    "2026-09-11",   # CPI agosto (estimado)
    "2026-10-13",   # CPI septiembre (estimado)
    "2026-11-13",   # CPI octubre (estimado)
    "2026-12-10",   # CPI noviembre (estimado)
} # --- FOMC (fuente: Federal Reserve, confirmadas) ---
    "2026-07-29",   # FOMC (decisión)
    "2026-09-16",   # FOMC (decisión, con dot plot)
    "2026-10-28",   # FOMC (decisión)
    "2026-12-09",   # FOMC (decisión)
    # --- CPI (8:30 ET, 2da semana; confirmá cada mes en bls.gov/schedule) ---
    "2026-08-12",   # CPI julio (estimado)
    "2026-09-11",   # CPI agosto (estimado)
    "2026-10-13",   # CPI septiembre (estimado)
    "2026-11-13",   # CPI octubre (estimado)
    "2026-12-10",   # CPI noviembre (estimado)
}
}

def is_nfp_day(d: date) -> bool:
    """NFP: primer viernes del mes (aproximación estándar)."""
    return d.weekday() == 4 and d.day <= 7

# ----------------------- TELEGRAM -----------------------
def tg(msg: str):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok:
        print("[TG]", msg); return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("telegram error:", e)

# ----------------------- ESTADO -----------------------
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"streak_losses": 0, "day": "", "day_losses": 0}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def log_trade(row: dict):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists: w.writeheader()
        w.writerow(row)

# ----------------------- DATA -----------------------
def fetch_5m(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, period="5d", interval="5m", prepost=True,
                     progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = df.index.tz_convert(ET)
    return df.dropna()

def fetch_daily(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, period="2y", interval="1d", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df.dropna()

# ----------------------- NIVELES Y RÉGIMEN -----------------------
def compute_levels(df5: pd.DataFrame, today: date) -> dict:
    rth = df5.between_time("09:30", "16:00")
    prev_days = sorted({d for d in rth.index.date if d < today})
    if not prev_days:
        return {}
    prev = prev_days[-1]
    prev_rth = rth[rth.index.date == prev]
    # overnight: desde el cierre del día previo hasta la apertura de hoy
    on = df5[(df5.index > prev_rth.index.max()) &
             (df5.index < datetime.combine(today, datetime.strptime("09:30", "%H:%M").time(), ET))]
    return {"pdh": float(prev_rth["high"].max()), "pdl": float(prev_rth["low"].min()),
            "onh": float(on["high"].max()) if len(on) else None,
            "onl": float(on["low"].min()) if len(on) else None}

def regime(daily: pd.DataFrame) -> str:
    hi, lo, cl = daily["high"], daily["low"], daily["close"]
    tr = np.maximum(hi - lo, np.maximum((hi - cl.shift()).abs(), (lo - cl.shift()).abs()))
    atr = tr.rolling(14).mean()
    pct = float(atr.rolling(252, min_periods=60).rank(pct=True).iloc[-1] * 100)
    if pct < ATR_LOW_PCT: return "no_trade"
    if pct > ATR_HIGH_PCT: return "half_size"
    return "normal"

# ----------------------- OPCIONES -----------------------
def bs_d1(S, K, T, iv): return (math.log(S / K) + (RF_RATE + 0.5 * iv**2) * T) / (iv * math.sqrt(T))

def suggest_spread(ticker: str, S: float, side: str) -> dict | None:
    """Elige vencimiento 1-2 DTE y strikes por delta usando la IV de la cadena real."""
    try:
        tk = yf.Ticker(ticker)
        today = datetime.now(ET).date()
        exps = [e for e in tk.options
                if 1 <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= 3]
        if not exps: return None
        exp = exps[0]
        T = max((datetime.strptime(exp, "%Y-%m-%d").date() - today).days, 1) / 252
        chain = tk.option_chain(exp)
        opts = chain.calls if side == "long" else chain.puts
        atm = opts.iloc[(opts["strike"] - S).abs().argsort()[:1]]
        iv = float(atm["impliedVolatility"].iloc[0]) or 0.2
        sign = 1 if side == "long" else -1
        k_long = min(opts["strike"], key=lambda k: abs(
            norm.cdf(sign * bs_d1(S, k, T, iv)) - LONG_DELTA))
        k_short = min(opts["strike"], key=lambda k: abs(
            norm.cdf(sign * bs_d1(S, k, T, iv)) - SHORT_DELTA))
        row_l = opts[opts.strike == k_long].iloc[0]
        row_s = opts[opts.strike == k_short].iloc[0]
        debit = float((row_l["bid"] + row_l["ask"]) / 2 - (row_s["bid"] + row_s["ask"]) / 2)
        return {"exp": exp, "k_long": float(k_long), "k_short": float(k_short),
                "debit_est": round(debit * 100, 0), "iv": round(iv, 3)}
    except Exception as e:
        print("chain error:", e); return None

# ----------------------- DETECCIÓN -----------------------
def check_signal(df5: pd.DataFrame, levels: dict, today: date) -> dict | None:
    """Aplica sweep -> contracción -> reclaim VWAP sobre las velas de hoy (RTH)."""
    day = df5.between_time("09:30", "16:00")
    day = day[day.index.date == today].copy()
    if len(day) < 3: return None
    tp = (day["high"] + day["low"] + day["close"]) / 3
    day["vwap"] = (tp * day["volume"]).cumsum() / day["volume"].cumsum()
    vol_ma = df5["volume"].rolling(20).mean().reindex(day.index)
    deadline = datetime.strptime(ENTRY_DEADLINE, "%H:%M").time()

    for side, pools, opp in (
        ("long",  [levels.get("pdl"), levels.get("onl")], [levels.get("pdh"), levels.get("onh")]),
        ("short", [levels.get("pdh"), levels.get("onh")], [levels.get("pdl"), levels.get("onl")]),
    ):
        pools = [p for p in pools if p]; opp = [p for p in opp if p]
        for i in range(len(day) - 1):
            b = day.iloc[i]
            if side == "long":
                swept = b["low"] < min(pools) * (1 - SWEEP_MIN_PCT) if pools else False
                back = b["close"] > min(pools) if pools else False
            else:
                swept = b["high"] > max(pools) * (1 + SWEEP_MIN_PCT) if pools else False
                back = b["close"] < max(pools) if pools else False
            vol_ok = pd.notna(vol_ma.iloc[i]) and b["volume"] >= VOL_MULT * vol_ma.iloc[i]
            if not (swept and back and vol_ok): continue
            nxt = day.iloc[i + 1 : i + 1 + LOOKAHEAD_BARS]
            if not len(nxt): continue
            if not (nxt.iloc[0]["high"] - nxt.iloc[0]["low"]) < (b["high"] - b["low"]): continue
            rec = nxt["close"] > nxt["vwap"] if side == "long" else nxt["close"] < nxt["vwap"]
            if not rec.any(): continue
            ts_entry = rec.idxmax()
            if ts_entry.time() > deadline: continue
            entry = float(nxt.loc[ts_entry, "close"])
            stop = float(b["low"] if side == "long" else b["high"])
            r = abs(entry - stop)
            if r <= 0: continue
            if side == "long":
                target = min([p for p in opp if p > entry] + [entry + TARGET_R * r])
            else:
                target = max([p for p in opp if p < entry] + [entry - TARGET_R * r])
            return {"side": side, "entry": entry, "stop": stop, "target": float(target),
                    "r": r, "ts": str(ts_entry)}
    return None

# ----------------------- GESTIÓN DE POSICIÓN -----------------------
def manage(ticker: str, sig: dict):
    """Monitorea la posición y alerta parcial/BE/stop/target hasta las 15:55 ET."""
    stop, partial_done = sig["stop"], False
    pxp = sig["entry"] + PARTIAL_AT_R * sig["r"] * (1 if sig["side"] == "long" else -1)
    while datetime.now(ET).time() < datetime.strptime("15:55", "%H:%M").time():
        time.sleep(150)
        try:
            px = float(yf.Ticker(ticker).history(period="1d", interval="1m")["Close"].iloc[-1])
        except Exception:
            continue
        long = sig["side"] == "long"
        if (px <= stop) if long else (px >= stop):
            res = "BE" if partial_done else "STOP -1R"
            tg(f"🛑 <b>{ticker} {res}</b> @ {px:.2f} — cerrá el resto del spread")
            return finish(ticker, sig, px, partial_done, stopped=True)
        if not partial_done and ((px >= pxp) if long else (px <= pxp)):
            partial_done, stop = True, sig["entry"]
            tg(f"✅ <b>{ticker} +1.5R</b> @ {px:.2f} — cerrá 50% y subí stop a BE ({stop:.2f})")
        if (px >= sig["target"]) if long else (px <= sig["target"]):
            tg(f"🎯 <b>{ticker} TARGET</b> @ {px:.2f} — cerrá todo")
            return finish(ticker, sig, px, partial_done, stopped=False)
    tg(f"⏰ <b>{ticker} cierre EOD</b> — cerrá el spread ahora (nunca overnight)")
    px = float(yf.Ticker(ticker).history(period="1d", interval="1m")["Close"].iloc[-1])
    return finish(ticker, sig, px, partial_done, stopped=False)

def finish(ticker, sig, px_exit, partial_done, stopped):
    long = sig["side"] == "long"
    rem_r = ((px_exit - sig["entry"]) / sig["r"]) * (1 if long else -1)
    r_total = (PARTIAL_FRAC * PARTIAL_AT_R if partial_done else 0) + \
              (1 - (PARTIAL_FRAC if partial_done else 0)) * (-1 if (stopped and not partial_done) else rem_r)
    st = load_state()
    if r_total < 0:
        st["streak_losses"] += 1; st["day_losses"] = st.get("day_losses", 0) + 1
    else:
        st["streak_losses"] = 0
    save_state(st)
    log_trade({"date": str(datetime.now(ET).date()), "ticker": ticker, **sig,
               "exit": px_exit, "r_total": round(r_total, 2)})
    tg(f"📋 {ticker} registrado: {r_total:+.2f}R | racha de pérdidas: {st['streak_losses']}")

# ----------------------- MAIN -----------------------
def main():
    now = datetime.now(ET)
    today = now.date()
    if now.weekday() >= 5: return

    st = load_state()
    if st.get("day") != str(today):
        st.update({"day": str(today), "day_losses": 0}); save_state(st)

    # --- filtros de día ---
    if str(today) in MACRO_DATES or is_nfp_day(today):
        tg("📅 Día macro (FOMC/CPI/NFP) — hoy no se opera. Ventana cerrada."); return
    reg = {}
    brief = [f"☀️ <b>Brief {today}</b> | equity ${float(os.environ.get('EQUITY', 9000)):,.0f} | riesgo/trade 20%"]
    data5, levels = {}, {}
    for tk_ in TICKERS:
        daily = fetch_daily(tk_)
        reg[tk_] = regime(daily)
        data5[tk_] = fetch_5m(tk_)
        levels[tk_] = compute_levels(data5[tk_], today)
        lv = levels[tk_]
        brief.append(f"{tk_}: {reg[tk_]} | PDH {lv.get('pdh'):.2f} PDL {lv.get('pdl'):.2f} "
                     f"ONH {lv.get('onh') or 0:.2f} ONL {lv.get('onl') or 0:.2f}")
    tg("\n".join(brief))
    if all(r == "no_trade" for r in reg.values()):
        tg("ATR en piso en todo el universo — no se opera hoy."); return

    # --- loop de detección hasta la deadline ---
    equity = float(os.environ.get("EQUITY", 9000))
    deadline = datetime.strptime(ENTRY_DEADLINE, "%H:%M").time()
    alerted = set()
    while datetime.now(ET).time() < deadline:
        st = load_state()
        if st["day_losses"] >= MAX_DAY_LOSSES:
            tg("🔒 2 pérdidas hoy — día terminado."); return
        for tk_ in TICKERS:
            if tk_ in alerted or reg[tk_] == "no_trade": continue
            try:
                data5[tk_] = fetch_5m(tk_)
            except Exception:
                continue
            sig = check_signal(data5[tk_], levels[tk_], today)
            if not sig: continue
            alerted.add(tk_)
            size_mult = 0.5 if (reg[tk_] == "half_size" or st["streak_losses"] >= 3) else 1.0
            spread = suggest_spread(tk_, sig["entry"], sig["side"])
            n = 1
            if spread and spread["debit_est"] > 0:
                n = max(int(equity * RISK_FRAC * size_mult // spread["debit_est"]), 1)
            veh = (f"\nSpread sugerido {spread['exp']}: {'CALL' if sig['side']=='long' else 'PUT'} "
                   f"{spread['k_long']:.0f}/{spread['k_short']:.0f} | débito ~${spread['debit_est']:.0f} "
                   f"| <b>cantidad: {n}</b> | IV {spread['iv']}") if spread else "\n(no pude leer la cadena — elegí strikes delta 0.50/0.28)"
            tg(f"🚨 <b>SEÑAL {tk_} {sig['side'].upper()}</b>\n"
               f"Entrada {sig['entry']:.2f} | Stop {sig['stop']:.2f} | Target {sig['target']:.2f} "
               f"(R = {sig['r']:.2f}){veh}\nOrden LIMIT al mid. Confirmá y aviso parcial/stop/target.")
            manage(tk_, sig)   # gestiona una posición por vez (regla: máx 2 correlación <0.5;
                               # en paper arrancamos con 1 por vez para limpieza del log)
            return
        time.sleep(120)
    tg("Ventana 9:30-10:00 cerrada sin señal. Hasta mañana.")

if __name__ == "__main__":
    main()
