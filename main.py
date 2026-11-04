from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER RANGE-FILTERED SWING BREAKOUT BACKTESTER ===")

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
# 2. BACKTEST ENGINE WITH RANGE CONDITION
# -------------------------------------------------------------
def backtest_range_filtered_swing(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    rr_target=3.0,
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
    n = len(df)

    if n < (min_consolidation_months * 2 + 5):
      return []

    # Range percentage calculation for every month: (High - Low) / Close * 100
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

    for i in range(min_consolidation_months + 3, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = df["Open"].iloc[i]
      curr_range = ranges[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss:
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100
          pnl_pct = (stop_loss - entry_price) / entry_price * 100
          trades.append({
              "Symbol": symbol,
              "Resistance": round(resistance_level, 2),
              "Breakout_Month": entry_date.strftime("%b %Y"),
              "Entry_Price": round(entry_price, 2),
              "Max_Profit_%": round(max_profit_pct, 2),
              "Result": "FAKEOUT / LOSS",
              "Final_PnL_%": round(pnl_pct, 2),
          })
          in_trade = False
        elif curr_high >= take_profit:
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100
          pnl_pct = (take_profit - entry_price) / entry_price * 100
          trades.append({
              "Symbol": symbol,
              "Resistance": round(resistance_level, 2),
              "Breakout_Month": entry_date.strftime("%b %Y"),
              "Entry_Price": round(entry_price, 2),
              "Max_Profit_%": round(max_profit_pct, 2),
              "Result": "WIN (TARGET HIT)",
              "Final_PnL_%": round(pnl_pct, 2),
          })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        risk = entry_price - stop_loss
        take_profit = entry_price + (risk * rr_target)
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False
        continue

      # Find Unbroken Swing High (Max High Month)
      active_resistance = None
      resistance_index = -1

      for j in range(
          i - min_consolidation_months, min_consolidation_months, -1
      ):
        is_valid_swing = True
        for k in range(j - min_consolidation_months, j):
          if highs[j] <= highs[k]:
            is_valid_swing = False
            break

        if is_valid_swing:
          ever_broken = False
          for k in range(j + 1, i):
            if highs[k] > highs[j]:
              ever_broken = True
              break
          if not ever_broken:
            active_resistance = highs[j]
            resistance_index = j
            break

      # Breakout Condition + Range Filter Logic:
      # 1. Close > Resistance
      # 2. Breakout candle range <= Max range of all months after the swing high up to current month
      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0:
          max_subsequent_range = post_swing_ranges.max()
          # Breakout month range should not be greater than the maximum range of post-swing months
          if curr_range <= max_subsequent_range:
            resistance_level = active_resistance
            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. EXECUTE & SAVE TO GOOGLE SHEETS
# -------------------------------------------------------------
all_trade_logs = []

if len(STOCKS) > 0:
  for idx, sym in enumerate(STOCKS, 1):
    print(f"[{idx}/{len(STOCKS)}] Running range-filtered backtest for {sym}...")
    t_logs = backtest_range_filtered_swing(sym)
    if t_logs:
      all_trade_logs.extend(t_logs)

if len(all_trade_logs) > 0:
  df_logs = pd.DataFrame(all_trade_logs)
  rows_logs = df_logs.values.tolist()

  try:
    ws_log = (
        sh.worksheet("RANGE_FILTER_LOGS")
        if "RANGE_FILTER_LOGS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("RANGE_FILTER_LOGS", 2000, 10)
    )
    ws_log.clear()
    ws_log.append_row([
        "RANGE FILTERED BREAKOUT LOGS |"
        f" {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"
    ])
    ws_log.append_row([
        "Stock Symbol",
        "Resistance Level",
        "Breakout Month",
        "Entry Price",
        "Max Profit % (High)",
        "Result Status",
        "Final PnL %",
    ])
    ws_log.append_rows(rows_logs, value_input_option="USER_ENTERED")
    print(
        "✅ Sheet 'RANGE_FILTER_LOGS' updated successfully with range-filtered"
        " trades!"
    )
  except Exception as e:
    print(f"Error updating Google Sheet: {e}")
else:
  print("⚠️ No trade logs found matching the range condition.")
  
