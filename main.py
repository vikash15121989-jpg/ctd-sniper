from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER EXACT MONTH MAPPING SCANNER ===")

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
# 2. SCANNER WITH EXACT MONTH-TO-MONTH LOGIC
# -------------------------------------------------------------
def scan_exact_month_breakout(symbol, min_consolidation_months=3):
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

    # n-2 is the LAST COMPLETED closed month (e.g., if today is Nov, n-2 is Oct)
    # But wait! As per your rule: September close -> October trade. 
    # So if we want signals for the UPCOMING/CURRENT trading month, we look at the candle that closed just before it.
    # Let's target the exact closed month index (n-2) and clearly label its intended Trading Month (n-1).
    
    i = n - 2  # Last completed closed month candle index
    closed_month_date = df.index[i]       # e.g., September candle close
    trading_month_date = df.index[i + 1]  # e.g., October trade execution month
    
    curr_close = closes[i]
    curr_range = ranges[i]

    # Find True Swing High prior to this closed month
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

    # Check if the closed month candle broke the active resistance
    if active_resistance is not None and curr_close > active_resistance:
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
        
        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol}", "{symbol} Chart")'
        
        signal_data = {
            "Symbol": symbol,
            "Scan_Date": datetime.now(IST).strftime("%d-%b-%Y"),
            "Breakout_Close_Month": closed_month_date.strftime("%b %Y"),  # e.g., Sep 2026
            "Trading_For_Month": trading_month_date.strftime("%b %Y"),    # e.g., Oct 2026
            "Trigger_Price": round(curr_close, 2),
            "Resistance_Broken": round(active_resistance, 2),
            "Stop_Loss (7%)": round(curr_close * 0.93, 2),
            "Take_Profit (14%)": round(curr_close * 1.14, 2),
            "TradingView": tv_link
        }
        print(f"🎯 [MATCH] {symbol}: Closed in {closed_month_date.strftime('%b %Y')} -> Valid for Trading in {trading_month_date.strftime('%b %Y')}")

    return signal_data

  except Exception as e:
    return None


# -------------------------------------------------------------
# 3. RUN SCANNER & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
exact_signals = []
if len(STOCKS) > 0:
  print(f"🔍 Scanning {len(STOCKS)} stocks with exact month mapping...")
  for sym in STOCKS:
    res = scan_exact_month_breakout(sym)
    if res:
      exact_signals.append(res)

print(f"\nTotal valid signals found: {len(exact_signals)}")

if len(exact_signals) > 0:
  df_ex = pd.DataFrame(exact_signals)
  try:
    ws = (
        sh.worksheet("EXACT_MAPPING_SIGNALS")
        if "EXACT_MAPPING_SIGNALS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("EXACT_MAPPING_SIGNALS", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"EXACT MONTH MAPPING SIGNALS | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Scan Date",
        "Breakout Close Month",
        "Trading For Month",
        "Trigger Price",
        "Resistance Broken",
        "Stop Loss (7%)",
        "Take Profit (14%)",
        "TradingView Link"
    ])
    ws.append_rows(df_ex.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Data successfully saved to 'EXACT_MAPPING_SIGNALS' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi matching breakout nahi mila.")
  
