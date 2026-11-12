from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER WEEKLY SUB-TIMEFRAME DIAGNOSTIC ===")

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
# 2. WEEKLY SUB-TIMEFRAME DIAGNOSTIC ENGINE
# -------------------------------------------------------------
def analyze_weekly_traits(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trade_features = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    
    # Download Monthly data for identifying breakout signals
    df_m = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    # Download Weekly data for sub-timeframe analysis
    df_w = yf.download(
        ticker_symbol, start="2012-01-01", interval="1wk", auto_adjust=False, progress=False
    )

    if df_m is None or df_m.empty or len(df_m) < 30 or df_w is None or df_w.empty:
      return trade_features

    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_w.columns, pd.MultiIndex):
      df_w.columns = df_w.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    df_w.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

    highs = df_m["High"].values
    lows = df_m["Low"].values
    closes = df_m["Close"].values
    opens = df_m["Open"].values
    volumes = df_m["Volume"].values
    n = len(df_m)

    df_m["Range_Pct"] = ((df_m["High"] - df_m["Low"]) / df_m["Close"]) * 100
    ranges = df_m["Range_Pct"].values

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    max_price_reached = 0

    for i in range(max(min_consolidation_months * 2 + 3, 10), n):
      current_date = df_m.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_range = ranges[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit

          if entry_date.year in [2025, 2026] or current_date.year in [2025, 2026]:
            # Extract weekly candles falling inside this breakout month
            month_start = pd.Timestamp(entry_date).replace(day=1)
            next_month = (month_start + pd.DateOffset(months=1))
            
            w_candles = df_w[(df_w.index >= month_start) & (df_w.index < next_month)]
            
            weekly_vol_ratio = 1.0
            weekly_close_consistency = 1.0
            
            if len(w_candles) > 0:
              # Check volume of the first weekly candle of the breakout month vs prior weekly average
              prior_w_vol = df_w.loc[df_w.index < month_start, "Volume"].tail(4).mean()
              first_w_vol = w_candles["Volume"].iloc[0]
              weekly_vol_ratio = first_w_vol / (prior_w_vol + 1)

              # Check how many weekly candles closed positive in that month
              pos_closes = sum(w_candles["Close"] > w_candles["Open"])
              weekly_close_consistency = pos_closes / len(w_candles)

            trade_features.append({
                "Symbol": symbol,
                "Result": "WIN" if is_win else "LOSS",
                "First_Weekly_Vol_Ratio": round(float(weekly_vol_ratio), 2),
                "Weekly_Positive_Ratio": round(float(weekly_close_consistency), 2)
            })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct)
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False
        continue

      # Find True Swing High
      active_resistance = None
      resistance_index = -1

      for j in range(
          i - min_consolidation_months, min_consolidation_months - 1, -1
      ):
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

      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
          pending_entry = True

    return trade_features

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT WEEKLY DIAGNOSTIC REPORT
# -------------------------------------------------------------
all_diag = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    res = analyze_weekly_traits(sym)
    if res:
      all_diag.extend(res)

if len(all_diag) > 0:
  df_diag = pd.DataFrame(all_diag)
  summary_weekly = df_diag.groupby("Result")[["First_Weekly_Vol_Ratio", "Weekly_Positive_Ratio"]].mean().reset_index()

  print(f"\n========================================")
  print(f" WEEKLY SUB-TIMEFRAME TRAITS ANALYSIS")
  print(f"========================================")
  print(summary_weekly.to_string(index=False))
  print(f"========================================")

  try:
    ws = (
        sh.worksheet("WEEKLY_DIAGNOSTIC")
        if "WEEKLY_DIAGNOSTIC" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("WEEKLY_DIAGNOSTIC", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"WEEKLY DIAGNOSTIC ANALYSIS (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(["Result Group", "Avg First Weekly Vol Ratio", "Avg Weekly Positive Ratio"])
    for _, row in summary_weekly.iterrows():
      ws.append_row([str(row["Result"]), float(row["First_Weekly_Vol_Ratio"]), float(row["Weekly_Positive_Ratio"])])
      
    ws.append_row([])
    ws.append_row(["--- RAW TRADES DATA ---"])
    ws.append_row(["Symbol", "Result", "First Weekly Vol Ratio", "Weekly Positive Ratio"])
    ws.append_rows(df_diag.values.tolist(), value_input_option="USER_ENTERED")
    
    print("✅ Weekly diagnostics successfully saved to 'WEEKLY_DIAGNOSTIC' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi data nahi mila weekly analysis ke liye.")
  
