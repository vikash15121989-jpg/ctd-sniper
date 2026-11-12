from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER WEEKLY FILTERED STRATEGY BACKTEST ===")

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
# 2. BACKTEST ENGINE WITH WEEKLY SUB-TIMEFRAME FILTERS
# -------------------------------------------------------------
def backtest_weekly_filtered(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    
    # Download Monthly & Weekly Data
    df_m = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    df_w = yf.download(
        ticker_symbol, start="2012-01-01", interval="1wk", auto_adjust=False, progress=False
    )

    if df_m is None or df_m.empty or len(df_m) < 30 or df_w is None or df_w.empty:
      return trades

    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_w.columns, pd.MultiIndex):
      df_w.columns = df_w.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    df_w.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

    df_m["SMA_10"] = df_m["Close"].rolling(window=10).mean()

    highs = df_m["High"].values
    lows = df_m["Low"].values
    closes = df_m["Close"].values
    opens = df_m["Open"].values
    volumes = df_m["Volume"].values
    sma10 = df_m["SMA_10"].values
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
      curr_sma10 = sma10[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              (take_profit - entry_price) / entry_price * 100
              if is_win
              else (stop_loss - entry_price) / entry_price * 100
          )
          max_profit_pct = ((max_price_reached - entry_price) / entry_price) * 100

          if entry_date.year in [2025, 2026] or current_date.year in [2025, 2026]:
            trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
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
          
          # -------------------------------------------------
          # WEEKLY SUB-TIMEFRAME FILTERS
          # -------------------------------------------------
          month_start = pd.Timestamp(current_date).replace(day=1)
          next_month = month_start + pd.DateOffset(months=1)
          w_candles = df_w[(df_w.index >= month_start) & (df_w.index < next_month)]

          weekly_vol_pass = True
          weekly_cons_pass = True

          if len(w_candles) > 0:
            prior_w_vol = df_w.loc[df_w.index < month_start, "Volume"].tail(4).mean()
            first_w_vol = w_candles["Volume"].iloc[0]
            weekly_vol_ratio = first_w_vol / (prior_w_vol + 1)

            pos_closes = sum(w_candles["Close"] > w_candles["Open"])
            weekly_positive_ratio = pos_closes / len(w_candles)

            # Filtering weak weekly volume and weak weekly consistency
            if weekly_vol_ratio < 1.0:
              weekly_vol_pass = False
            if weekly_positive_ratio < 0.4:
              weekly_cons_pass = False

          # Monthly Base Filter + Weekly Sub-Timeframe Confirmation
          candle_span = curr_high - curr_low
          close_loc = (curr_close - curr_low) / (candle_span + 0.0001)
          sma_dist = ((curr_close - curr_sma10) / curr_sma10) * 100 if not np.isnan(curr_sma10) else 0

          if close_loc >= 0.5 and sma_dist >= 15.0 and weekly_vol_pass and weekly_cons_pass:
            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_weekly_filtered(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print(f"\n========================================")
  print(f" WEEKLY FILTERED STRATEGY RESULTS (2025-2026)")
  print(f"========================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"========================================")

  try:
    ws = (
        sh.worksheet("WEEKLY_FILTERED_LOG")
        if "WEEKLY_FILTERED_LOG" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("WEEKLY_FILTERED_LOG", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"WEEKLY FILTERED LOG (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Entry Date",
        "Entry Price",
        "Max Profit %",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Data successfully saved to 'WEEKLY_FILTERED_LOG' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
