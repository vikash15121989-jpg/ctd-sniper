from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER WINNER VS LOSER DIAGNOSTIC SCANNER ===")

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
# 2. DIAGNOSTIC COMPARISON ENGINE
# -------------------------------------------------------------
def analyze_winner_loser_traits(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trade_features = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    df_w = yf.download(
        ticker_symbol, start="2023-01-01", interval="1wk", auto_adjust=False, progress=False
    )

    if df is None or df.empty or len(df) < 30 or df_w is None or df_w.empty:
      return trade_features

    for d in [df, df_w]:
      if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
      d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    volumes = df["Volume"].values
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    max_price_reached = 0

    for i in range(min_consolidation_months * 2 + 3, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_range = ranges[i]
      curr_vol = volumes[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          
          if entry_date.year in [2025, 2026] or current_date.year in [2025, 2026]:
            w_after = df_w[df_w.index >= entry_date]
            avg_weekly_vol_surge = 0
            if len(w_after) >= 2:
              w_vols = w_after["Volume"].values[:2]
              avg_weekly_vol_surge = np.mean(w_vols) / (np.mean(volumes[max(0, i-3):i]) + 1)

            trade_features.append({
                "Symbol": symbol,
                "Result": "WIN" if is_win else "LOSS",
                "Breakout_Volume_Ratio": round(curr_vol / (np.mean(volumes[i-3:i]) + 1), 2),
                "Candle_Close_Location": round((curr_close - curr_low) / (curr_high - curr_low + 0.0001), 2),
                "Early_Weekly_Vol_Ratio": round(float(avg_weekly_vol_surge), 2)
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
# 3. RUN & EXPORT COMPARISON METRICS TO SHEET
# -------------------------------------------------------------
all_diagnostics = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    res = analyze_winner_loser_traits(sym)
    if res:
      all_diagnostics.extend(res)

if len(all_diagnostics) > 0:
  df_diag = pd.DataFrame(all_diagnostics)
  
  summary_stats = df_diag.groupby("Result")[["Breakout_Volume_Ratio", "Candle_Close_Location", "Early_Weekly_Vol_Ratio"]].mean().reset_index()

  print(f"\n========================================")
  print(f" WINNER VS LOSER BEHAVIORAL COMPARISON")
  print(f"========================================")
  print(summary_stats.to_string(index=False))
  print(f"========================================")

  try:
    ws = (
        sh.worksheet("WINNER_VS_LOSER_DIAGNOSTIC")
        if "WINNER_VS_LOSER_DIAGNOSTIC" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("WINNER_VS_LOSER_DIAGNOSTIC", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"WINNER VS LOSER DIAGNOSTIC ANALYSIS (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(["Result Group", "Avg Breakout Volume Ratio", "Avg Candle Close Location", "Early Weekly Volume Ratio"])
    for _, row in summary_stats.iterrows():
      ws.append_row([str(row["Result"]), float(row["Breakout_Volume_Ratio"]), float(row["Candle_Close_Location"]), float(row["Early_Weekly_Vol_Ratio"])])
      
    ws.append_row([])
    ws.append_row(["--- RAW TRADE TRAITS ---"])
    ws.append_row(["Symbol", "Result", "Breakout Volume Ratio", "Candle Close Location", "Early Weekly Vol Ratio"])
    ws.append_rows(df_diag.values.tolist(), value_input_option="USER_ENTERED")
    
    print("✅ Diagnostic metrics successfully saved to 'WINNER_VS_LOSER_DIAGNOSTIC' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi data nahi mila analysis ke liye.")
  
