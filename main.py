from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER EXACT TRADE-BY-TRADE TIMEFRAME SCANNER ===")

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
# 2. EXACT TIMEFRAME DIAGNOSTIC ENGINE (NO AVERAGES)
# -------------------------------------------------------------
def analyze_exact_trades(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  detailed_trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    
    # Download Monthly, Weekly, and Daily Data
    df_m = yf.download(ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False)
    df_w = yf.download(ticker_symbol, start="2012-01-01", interval="1wk", auto_adjust=False, progress=False)
    df_d = yf.download(ticker_symbol, start="2012-01-01", interval="1d", auto_adjust=False, progress=False)

    if df_m is None or df_m.empty or len(df_m) < 30 or df_w is None or df_d is None:
      return detailed_trades

    for df_frame in [df_m, df_w, df_d]:
      if isinstance(df_frame.columns, pd.MultiIndex):
        df_frame.columns = df_frame.columns.get_level_values(0)
      df_frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

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

          if entry_date.year in [2025, 2026] or current_date.year in [2025, 2026]:
            month_start = pd.Timestamp(entry_date).replace(day=1)
            next_month = month_start + pd.DateOffset(months=1)
            
            # Weekly exact metrics for this breakout month
            w_candles = df_w[(df_w.index >= month_start) & (df_w.index < next_month)]
            w_vol_ratio = 0.0
            w_pos_ratio = 0.0
            if len(w_candles) > 0:
              prior_w_vol = df_w.loc[df_w.index < month_start, "Volume"].tail(4).mean()
              w_vol_ratio = round(float(w_candles["Volume"].iloc[0] / (prior_w_vol + 1)), 2)
              w_pos_ratio = round(float(sum(w_candles["Close"] > w_candles["Open"]) / len(w_candles)), 2)

            # Daily exact metrics during the breakout month (first 10 trading days or breakout window)
            d_candles = df_d[(df_d.index >= month_start) & (df_d.index < next_month)]
            d_upper_wicks_avg = 0.0
            if len(d_candles) > 0:
              # Upper wick as % of daily range
              upper_wicks = (d_candles["High"] - pd.concat([d_candles["Close"], d_candles["Open"]], axis=1).max(axis=1)) / (d_candles["High"] - d_candles["Low"] + 0.0001)
              d_upper_wicks_avg = round(float(upper_wicks.mean()), 2)

            detailed_trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Result": "WIN" if is_win else "LOSS",
                "Monthly_Close_Loc": round(float((curr_close - curr_low) / (curr_high - curr_low + 0.0001)), 2),
                "Weekly_First_Vol_Ratio": w_vol_ratio,
                "Weekly_Positive_Candle_Ratio": w_pos_ratio,
                "Daily_Upper_Wick_Pressure": d_upper_wicks_avg,
                "PnL_%": round(pnl_pct, 2)
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

    return detailed_trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT EXACT TRADE LOG TO GOOGLE SHEET
# -------------------------------------------------------------
all_exact_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    res = analyze_exact_trades(sym)
    if res:
      all_exact_trades.extend(res)

if len(all_trades_count := all_exact_trades) > 0:
  df_exact = pd.DataFrame(all_exact_trades)
  
  print(f"\n========================================")
  print(f" EXACT TRADE-BY-TRADE LOG GENERATED: {len(df_exact)} trades")
  print(f"========================================")

  try:
    ws = (
        sh.worksheet("EXACT_TRADE_LOG")
        if "EXACT_TRADE_LOG" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("EXACT_TRADE_LOG", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"EXACT TRADE-BY-TRADE TIMEFRAME LOG (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Entry Date",
        "Result",
        "Monthly Close Loc",
        "Weekly First Vol Ratio",
        "Weekly Positive Candle Ratio",
        "Daily Upper Wick Pressure",
        "PnL %"
    ])
    ws.append_rows(df_exact.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Exact trade-by-trade data successfully saved to 'EXACT_TRADE_LOG' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
      
