from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER LIVE BREAKOUT SCANNER ===")

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & WATCHLIST LOAD
# -------------------------------------------------------------
try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = [
      s.strip().upper()
      for s in raw
      if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]
  ]
  print(f"✅ Loaded {len(STOCKS)} stocks from Watchlist for Live Scanning.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


# -------------------------------------------------------------
# 2. LIVE SCANNER ENGINE (CURRENT MONTH BREAKOUT DETECTION)
# -------------------------------------------------------------
def scan_live_breakouts(symbol, min_consolidation_months=3):
  signal_data = None
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )

    if df is None or df.empty or len(df) < 30:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    # Check the very last candle (Current Active/Latest Month)
    i = n - 1
    current_date = df.index[i]
    curr_close = closes[i]
    curr_high = highs[i]
    curr_low = lows[i]
    curr_open = opens[i]
    curr_range = ranges[i]

    # Find True Swing High in history prior to current month
    active_resistance = None
    resistance_index = -1

    for j in range(i - min_consolidation_months, min_consolidation_months - 1, -1):
      is_valid_swing = True
      for k in range(j - min_consolidation_months, j):
        if highs[j] <= highs[k]:
          is_valid_swing = False
          break
      if not is_valid_swing:
        continue

      for k in range(j + 1, j + min_consolidation_months + 1):
        if highs[j] <= highs[k]:
          is_valid_swing = False
          break
      if not is_valid_swing:
        continue

      ever_broken = False
      for k in range(j + 1, i):
        if highs[k] > highs[j]:
          ever_broken = True
          break
      if ever_broken:
        continue

      active_resistance = highs[j]
      resistance_index = j
      break

    # Check if current month breaks the active resistance (Pure Breakout)
    if active_resistance is not None and curr_close > active_resistance:
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
        
        # Generate TradingView Clickable Chart Hyperlink
        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol}", "{symbol} Chart")'
        
        signal_data = {
            "Symbol": symbol,
            "Scan_Date": datetime.now(IST).strftime("%d-%b-%Y"),
            "Breakout_Month": current_date.strftime("%b %Y"),
            "Trigger_Price": round(curr_close, 2),
            "Resistance_Broken": round(active_resistance, 2),
            "Stop_Loss (7%)": round(curr_close * 0.93, 2),
            "Take_Profit (14%)": round(curr_close * 1.14, 2),
            "TradingView": tv_link
        }

    return signal_data

  except Exception as e:
    return None


# -------------------------------------------------------------
# 3. RUN SCANNER & EXPORT SIGNALS TO GOOGLE SHEET
# -------------------------------------------------------------
live_signals = []
if len(STOCKS) > 0:
  print("🔍 Scanning watchlist for active breakouts...")
  for sym in STOCKS:
    res = scan_live_breakouts(sym)
    if res:
      live_signals.append(res)

if len(live_signals) > 0:
  df_live = pd.DataFrame(live_signals)
  print(f"\n========================================")
  print(f" LIVE SCANNER FOUND {len(df_live)} BREAKOUT SIGNALS")
  print(f"========================================")
  print(df_live[["Symbol", "Trigger_Price", "Stop_Loss (7%)", "Take_Profit (14%)"]])

  try:
    ws = (
        sh.worksheet("LIVE_SCANNER_SIGNALS")
        if "LIVE_SCANNER_SIGNALS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("LIVE_SCANNER_SIGNALS", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"LIVE BREAKOUT SIGNALS | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"
    ])
    ws.append_row([
        "Symbol",
        "Scan Date",
        "Breakout Month",
        "Trigger Price",
        "Resistance Broken",
        "Stop Loss (7%)",
        "Take Profit (14%)",
        "TradingView Link"
    ])
    ws.append_rows(df_live.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Live signals successfully saved to 'LIVE_SCANNER_SIGNALS' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Current active candle par koi naya breakout match nahi mila.")
