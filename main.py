from datetime import datetime, timedelta, timezone
import json
import math
import os
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

# IST Timezone setup
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(
    f"=== [LIVE CTD_SNIPER: RS + 20 EMA SHAKEOUT SCANNER] |"
    f" {now.strftime('%d-%b-%Y %H:%M IST')} ===",
    flush=True,
)


def sanitize_value(val):
  if isinstance(val, float):
    if math.isnan(val) or math.isinf(val):
      return 0.0
  return val


def sanitize_rows(rows):
  return [[sanitize_value(v) for v in row] for row in rows]


# Google Sheets Connection
try:
  gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
  gc = gspread.service_account_from_dict(gcp_json_creds)
  sh = gc.open("CTD_Sniper")
  print("✅ Connected to Google Sheets!", flush=True)
except Exception as e:
  print(f"❌ Connection Error: {e}")
  exit(1)


def get_or_create_worksheet(title):
  try:
    return sh.worksheet(title)
  except gspread.exceptions.WorksheetNotFound:
    return sh.add_worksheet(title=title, rows="1000", cols="15")


ETF_KEYWORDS = [
    "BEES",
    "ETF",
    "GOLD",
    "SILVER",
    "NIFTY",
    "NAV",
    "LIQUID",
    "IETF",
    "HANGSENG",
    "SENSEX",
    "MON100",
]


def is_etf(s):
  return any(kw in s.upper() for kw in ETF_KEYWORDS)


# 1. Read Watchlist from Sheet
try:
  raw_stocks = sh.worksheet("Watchlist").col_values(1)
except Exception:
  print("⚠️ 'Watchlist' tab not found in Google Sheet!")
  exit(1)

STOCKS = []
for s in raw_stocks:
  if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
    clean_s = s.strip().upper()
    if not clean_s.endswith(".NS"):
      clean_s += ".NS"
    STOCKS.append(clean_s)

clean_stocks = list(
    set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))])
)
print(
    f"📥 Scanning {len(clean_stocks)} stocks with Advanced RS + Shakeout"
    " Logic...",
    flush=True,
)

# Fetch Benchmark Nifty Data
try:
  nifty_df = yf.Ticker("^NSEI").history(
      period="2y", interval="1d", auto_adjust=False
  )
  nifty_df["20_EMA"] = nifty_df["Close"].ewm(span=20, adjust=False).mean()
except Exception as e:
  print(f"❌ Error fetching Nifty Index data: {e}")
  exit(1)

live_signals = []

for symbol in clean_stocks:
  try:
    stock_clean = symbol.replace(".NS", "")
    data = yf.Ticker(symbol).history(
        period="2y", interval="1d", auto_adjust=False
    )
    if data.empty or len(data) < 200:
      continue

    if isinstance(data.columns, pd.MultiIndex):
      data.columns = data.columns.get_level_values(0)

    # Daily Turnover Check (>= ₹2 Cr)
    data["Turnover"] = data["Close"] * data["Volume"]
    recent_daily_turnover = data["Turnover"].iloc[-20:].mean()
    if recent_daily_turnover < 20000000:
      continue

    # EMA & Technicals on Daily Chart
    data["20_EMA"] = data["Close"].ewm(span=20, adjust=False).mean()

    # Align with Nifty for Mansfield Relative Strength (RS)
    combined = pd.DataFrame(
        {"Stock": data["Close"], "Nifty": nifty_df["Close"]}
    ).dropna()
    rel_ratio = combined["Stock"] / combined["Nifty"]
    base_ma = rel_ratio.rolling(window=50).mean()
    mansfield_rs = ((rel_ratio / base_ma) - 1) * 100
    latest_rs = mansfield_rs.iloc[-1] if not mansfield_rs.empty else -99

    # 20 EMA Shakeout & Reclaim Check (Did stock dip below/touch 20 EMA in last 10 days and reclaim?)
    recent_lows = data["Low"].iloc[-10:]
    recent_emas = data["20_EMA"].iloc[-10:]
    had_shakeout = any(recent_lows <= recent_emas * 1.01)
    currently_above_ema = data["Close"].iloc[-1] > data["20_EMA"].iloc[-1]

    # Resample to Monthly Candles
    monthly = (
        data.resample("ME")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })
        .dropna()
    )

    if len(monthly) < 6:
      continue

    current_year = now.year
    current_month = now.month
    completed_monthly = monthly[
        ~(
            (monthly.index.year == current_year)
            & (monthly.index.month == current_month)
        )
    ]

    if len(completed_monthly) < 5:
      continue

    setup_month = completed_monthly.iloc[-1]
    prev_month = completed_monthly.iloc[-2]
    prev_4_vols = completed_monthly["Volume"].iloc[-5:-1]

    # STRICT FILTER CONDITIONS
    cond_close_above_high = setup_month["Close"] > prev_month["High"]
    cond_volume_surge = setup_month["Volume"] > prev_4_vols.max()
    cond_rs_positive = latest_rs > 0  # Relative Strength vs Nifty
    cond_ema_shakeout = had_shakeout and currently_above_ema

    if (
        cond_close_above_high
        and cond_volume_surge
        and cond_rs_positive
        and cond_ema_shakeout
    ):
      entry_level = round(setup_month["High"], 2)

      # Dynamic Stoploss: Lower of Setup Month Low OR 20 EMA (Max 5% Cap)
      shakeout_sl = min(
          setup_month["Low"], data["20_EMA"].iloc[-1] * 0.98
      )  # 2% below 20 EMA
      stoploss_level = round(max(shakeout_sl, entry_level * 0.95), 2)

      target_6_pct = round(entry_level * 1.06, 2)
      target_10_pct = round(entry_level * 1.10, 2)

      chart_link_formula = (
          '=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:'
          f'{stock_clean}", "📊 Open Chart")'
      )

      live_signals.append([
          stock_clean,
          setup_month.name.strftime("%Y-%m"),
          entry_level,
          target_6_pct,
          target_10_pct,
          stoploss_level,
          round(setup_month["Close"], 2),
          round(latest_rs, 2),
          round(recent_daily_turnover / 10000000, 2),
          chart_link_formula,
      ])

  except Exception:
    continue

print(f"🚀 Valid Sniper Breakouts Found: {len(live_signals)}", flush=True)

# 2. Export Live Signals to Google Sheet
ws_live = get_or_create_worksheet("Live_Breakout_Signals")
ws_live.clear()

headers = [
    "Stock",
    "Setup_Month",
    "Entry_Trigger",
    "Target_6%",
    "Target_10%",
    "StopLoss",
    "Setup_Close",
    "RS_Score",
    "Avg_Daily_Turnover_Cr",
    "TradingView_Chart",
]
ws_live.append_row(headers, value_input_option="USER_ENTERED")

if live_signals:
  ws_live.append_rows(
      sanitize_rows(live_signals), value_input_option="USER_ENTERED"
  )
  print("✅ Signals successfully exported with 6%, 10% targets & Shakeout SL!")
else:
  ws_live.append_row(
      [
          "No stocks matching strict criteria today.",
          "",
          "",
          "",
          "",
          "",
          "",
          "",
          "",
          "",
      ],
      value_input_option="USER_ENTERED",
  )
  print("ℹ️ No stocks matched the criteria today.")
