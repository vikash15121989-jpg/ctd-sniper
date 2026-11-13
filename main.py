from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER LIVE MAJOR SWING SCANNER (413-TRADE LOGIC) ===")

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
# 2. LIVE SCANNER FUNCTION (MAJOR SWING 413-TRADE ENGINE)
# -------------------------------------------------------------
def scan_live_major_swing(symbol, min_consolidation_months=3):
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    # Fetch monthly data from 2012 onwards
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )

    if df is None or df.empty or len(df) < 30:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    closes = df["Close"].values
    dates = df.index
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    # We check the latest closed candle (penultimate index or last completed month)
    # i = n - 2 represents the last fully closed monthly candle that triggered the breakout into current month
    i = n - 2
    if i < min_consolidation_months * 2 + 3:
      return None

    signal_date = dates[i]
    curr_close = closes[i]
    curr_range = ranges[i]

    # MAJOR SWING DETECTION LOGIC
    active_resistance = None
    resistance_index = -1

    search_range = range(
        i - min_consolidation_months, min_consolidation_months - 1, -1
    )

    for j in search_range:
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

    # Check if breakout occurred on this latest closed candle
    if active_resistance is not None and curr_close > active_resistance:
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_swing_ranges) > 0:
        if curr_range <= post_swing_ranges.max():
          # Breakout confirmed! Calculate live levels
          entry_price = closes[i]  # Breakout close / Live reference price
          stop_loss = round(entry_price * 0.93, 2)  # -7% SL
          take_profit = round(entry_price * 1.14, 2)  # +14% TP

          tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
          sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

          return {
              "Symbol": symbol,
              "Signal_Month": signal_date.strftime("%b %Y"),
              "Resistance": round(active_resistance, 2),
              "Trigger_Price": round(entry_price, 2),
              "Stop_Loss (-7%)": stop_loss,
              "Take_Profit (+14%)": take_profit,
              "TradingView_Chart": sheet_link_formula,
          }

  except Exception as e:
    pass

  return None


# -------------------------------------------------------------
# 3. EXECUTE & EXPORT LIVE SIGNALS TO GOOGLE SHEETS
# -------------------------------------------------------------
live_signals = []
if len(STOCKS) > 0:
  print("🔍 Scanning live market for Major Swing breakouts...")
  for sym in STOCKS:
    signal = scan_live_major_swing(sym)
    if signal:
      live_signals.append(signal)

if len(live_signals) > 0:
  df_live = pd.DataFrame(live_signals)
  print(f"🚀 Found {len(live_signals)} active live breakout signals!")

  tab_name = "LIVE_MAJOR_SIGNALS"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "LIVE MAJOR SWING BREAKOUT SIGNALS | Generated on"
        f" {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal Month",
        "Resistance",
        "Trigger Price",
        "Stop Loss (-7%)",
        "Take Profit (+14%)",
        "TradingView Link",
    ])
    ws.append_rows(df_live.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Successfully updated '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
    print("⚠️ Is samay koi naya live breakout signal nahi mila.")
  
