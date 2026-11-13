from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER FULLY AUTOMATED DYNAMIC BREAKOUT SCANNER ===")

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
  print(f"✅ Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


# -------------------------------------------------------------
# 2. DYNAMIC SCANNER FOR THE LAST COMPLETED MONTH'S BREAKOUT
# -------------------------------------------------------------
def scan_latest_completed_month_breakout(symbol, min_consolidation_months=3):
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
    n = len(df)

    df["Range_Ratio"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Ratio"].values

    # Target the LAST COMPLETED monthly candle (n-2 index, because n-1 is the current running month)
    i = n - 2
    current_date = df.index[i]
    curr_close = closes[i]
    curr_range = ranges[i]

    # Find True Swing High prior to this completed month
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

    # Check if that completed month closed above the active resistance
    if active_resistance is not None and curr_close > active_resistance:
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
        
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
# 3. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
latest_signals = []
if len(STOCKS) > 0:
  print(f"🔍 Scanning watchlist for last closed month breakouts...")
  for sym in STOCKS:
    res = scan_latest_completed_month_breakout(sym)
    if res:
      latest_signals.append(res)

if len(latest_signals) > 0:
  df_lat = pd.DataFrame(latest_signals)
  print(f"\n========================================")
  print(f" LATEST CLOSED MONTH BREAKOUTS FOUND: {len(df_lat)}")
  print(f"========================================")
  print(df_lat[["Symbol", "Breakout_Month", "Trigger_Price", "Stop_Loss (7%)", "Take_Profit (14%)"]])

  try:
    ws = (
        sh.worksheet("LATEST_BREAKOUT_SIGNALS")
        if "LATEST_BREAKOUT_SIGNALS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("LATEST_BREAKOUT_SIGNALS", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"LATEST CLOSED MONTH BREAKOUT SIGNALS | {datetime.now(IST).strftime('%d-%b-%Y')}"
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
    ws.append_rows(df_lat.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Data successfully saved to 'LATEST_BREAKOUT_SIGNALS' tab in Google Sheet!")
  exceptException as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Pichle closed mahine me koi breakout match nahi mila.")
  
