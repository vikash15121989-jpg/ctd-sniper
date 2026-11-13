from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER STRICT NEXT-MONTH ENTRY SCANNER ===")

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
# 2. SCANNER WITH STRICT NEXT-MONTH MAPPING
# -------------------------------------------------------------
def scan_next_month_entry(symbol, min_consolidation_months=3):
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

    # STRICT MAPPING:
    # n-2 index is the last fully completed closed month (e.g., October close)
    # n-1 index is the immediate next month where trade/entry is taken (e.g., November entry)
    i = n - 2  
    breakout_month_date = df.index[i]     # Mahina jisme candle close hui (e.g. Oct 2026)
    entry_month_date = df.index[i + 1]    # Agla mahina jisme entry leni hai (e.g. Nov 2026)
    
    curr_close = closes[i]
    curr_range = ranges[i]

    # Find True Swing High prior to the breakout month
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

    # Check if breakout month closed above active resistance
    if active_resistance is not None and curr_close > active_resistance:
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_sighting := post_swing_ranges) > 0 and curr_range <= post_sighting.max():
        
        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol}", "{symbol} Chart")'
        
        signal_data = {
            "Symbol": symbol,
            "Scan_Date": datetime.now(IST).strftime("%d-%b-%Y"),
            "Breakout_Close_Month": breakout_month_date.strftime("%b %Y"),
            "Entry_Trading_Month": entry_month_date.strftime("%b %Y"),
            "Trigger_Price": round(curr_close, 2),
            "Resistance_Broken": round(active_resistance, 2),
            "Stop_Loss (7%)": round(curr_close * 0.93, 2),
            "Take_Profit (14%)": round(curr_close * 1.14, 2),
            "TradingView": tv_link
        }
        print(f"🎯 [MATCH] {symbol} | Close: {breakout_month_date.strftime('%b %Y')} ➡️ Entry Valid For: {entry_month_date.strftime('%b %Y')}")

    return signal_data

  except Exception as e:
    return None


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_signals = []
if len(STOCKS) > 0:
  print(f"🔍 Scanning {len(STOCKS)} stocks with strict next-month entry rule...")
  for sym in STOCKS:
    res = scan_next_month_entry(sym)
    if res:
      all_signals.append(res)

print(f"\nTotal valid signals found: {len(all_signals)}")

if len(all_signals) > 0:
  df_res = pd.DataFrame(all_signals)
  # Target tab name based strictly on the Entry Trading Month
  target_tab = df_res['Entry_Trading_Month'].iloc[0].replace(" ", "_").upper() + "_SIGNALS"
  
  try:
    ws = (
        sh.worksheet(target_tab)
        if target_tab in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(target_tab, 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"STRICT NEXT-MONTH ENTRY SIGNALS | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Scan Date",
        "Breakout Close Month",
        "Entry Trading Month",
        "Trigger Price",
        "Resistance Broken",
        "Stop Loss (7%)",
        "Take Profit (14%)",
        "TradingView Link"
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{target_tab}' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi matching setup nahi mila.")
  
