from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MULTI-LOGIC EDGE DISCOVERY (2025-2026) ===")

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
# 2. EDGE DISCOVERY ENGINE
# -------------------------------------------------------------
def discover_edge(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False
    )

    if df is None or df.empty or len(df) < 30:
      return []

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    trades = []
    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    resistance_level = 0
    max_price_reached = 0

    # Metrics to capture for Edge analysis
    close_location_pct = (
        0  # Where did breakout close relative to its high-low range (0 to 1)
    )
    base_drawdown_pct = 0  # Max drop inside the base
    distance_from_52w_high = (
        0  # How close was resistance to 52-week high at breakout
    )

    for i in range(min_consolidation_months * 2 + 3, n):
      current_date = df.index[i]
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
          pnl_pct = (
              (take_profit - entry_price) / entry_price * 100
              if is_win
              else (stop_loss - entry_price) / entry_price * 100
          )

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Close_Location": round(close_location_pct, 2),
                "Base_Drawdown_%": round(base_drawdown_pct, 2),
                "Dist_52W_High_%": round(distance_from_52w_high, 2),
                "Result": "WIN" if is_win else "LOSS",
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
        if len(post_swing_ranges) > 0:
          max_subsequent_range = post_swing_ranges.max()
          if curr_range <= max_subsequent_range:
            resistance_level = active_resistance

            # Calculate Edge Metrics
            # 1. Close Location in Breakout Candle (0 = Low, 1 = High)
            candle_span = curr_high - curr_low
            close_location_pct = (
                (curr_close - curr_low) / candle_span if candle_span > 0 else 0
            )

            # 2. Base Drawdown (Max drop from resistance to lowest low inside base)
            base_lows = lows[resistance_index + 1 : i]
            base_drawdown_pct = (
                ((active_resistance - base_lows.min()) / active_resistance)
                * 100
                if len(base_lows) > 0
                else 0
            )

            # 3. Distance from 52W High at breakout time
            rolling_52w_high = highs[max(0, i - 12) : i].max()
            distance_from_52w_high = (
                ((rolling_52w_high - active_resistance) / rolling_52w_high)
                * 100
            )

            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & COMPARE EDGES
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = discover_edge(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)

  print("\n==================================================")
  print(" MULTI-LOGIC EDGE DISCOVERY REPORT")
  print("==================================================")
  wins_df = df_res[df_res["Result"] == "WIN"]
  loss_df = df_res[df_res["Result"] == "LOSS"]

  print(f"Total Wins Analysed  : {len(wins_df)}")
  print(f"Total Losses Analysed: {len(loss_df)}")
  print("--------------------------------------------------")
  print(
      "🎯 Close Location (High ke paas close): Wins:"
      f" {wins_df['Close_Location'].mean():.2f} | Losses:"
      f" {loss_df['Close_Location'].mean():.2f}"
  )
  print(
      "🎯 Base Drawdown % (Kitna gira base me): Wins:"
      f" {wins_df['Base_Drawdown_%'].mean():.2f}% | Losses:"
      f" {loss_df['Base_Drawdown_%'].mean():.2f}%"
  )
  print(
      "🎯 Distance from 52W High %: Wins:"
      f" {wins_df['Dist_52W_High_%'].mean():.2f}% | Losses:"
      f" {loss_df['Dist_52W_High_%'].mean():.2f}%"
  )
  print("==================================================")

  try:
    ws = (
        sh.worksheet("EDGE_DISCOVERY")
        if "EDGE_DISCOVERY" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("EDGE_DISCOVERY", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "MULTI-LOGIC EDGE REPORT |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Stock Symbol",
        "Close Location",
        "Base Drawdown %",
        "Dist 52W High %",
        "Result",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Edge discovery report saved to 'EDGE_DISCOVERY' tab in sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
