from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MAX PROFIT & FAKEOUT BACKTESTER ===")

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
# 2. TRADE LOGS WITH MAX PROFIT & FAKEOUT TRACKING
# -------------------------------------------------------------
def backtest_max_profit_and_fakeout(
    symbol,
    min_consolidation_months=3,
    atr_period=14,
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
    closes = df["Close"].values
    lows = df["Low"].values
    n = len(df)

    if n < (min_consolidation_months * 2 + atr_period):
      return []

    # ATR Calculation
    tr1 = df["High"] - df["Low"]
    tr2 = np.abs(df["High"] - df["Close"].shift(1))
    tr3 = np.abs(df["Low"] - df["Close"].shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(window=atr_period).mean()
    df["ATR_Pct"] = (df["ATR"] / df["Close"]) * 100
    median_atr_pct = df["ATR_Pct"].median()

    trades = []
    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    resistance_level = 0
    max_price_reached = 0

    for i in range(min_consolidation_months + atr_period, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = df["Open"].iloc[i]
      atr_p = df["ATR_Pct"].iloc[i]

      if in_trade:
        # Track maximum price reached during the trade
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        # Check exit conditions
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
              "Result": (
                  "FAKEOUT / LOSS" if max_profit_pct < 5 else "PARTIAL / LOSS"
              ),
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

      # Find Unbroken Swing High
      active_resistance = None
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
            break

      # Breakout & Strict ATR Filter
      if (
          active_resistance is not None
          and curr_close > active_resistance
          and pd.notna(atr_p)
          and atr_p <= median_atr_pct
      ):
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
    print(f"[{idx}/{len(STOCKS)}] Analyzing max profit & fakeouts for {sym}...")
    t_logs = backtest_max_profit_and_fakeout(sym)
    if t_logs:
      all_trade_logs.extend(t_logs)

if len(all_trade_logs) > 0:
  df_logs = pd.DataFrame(all_trade_logs)
  rows_logs = df_logs.values.tolist()

  try:
    ws_log = (
        sh.worksheet("TRADE_FAKEOUT_LOGS")
        if "TRADE_FAKEOUT_LOGS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("TRADE_FAKEOUT_LOGS", 2000, 10)
    )
    ws_log.clear()
    ws_log.append_row([
        "BREAKOUT MAX PROFIT & FAKEOUT ANALYSIS |"
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
        "✅ Sheet 'TRADE_FAKEOUT_LOGS' updated successfully with max profit and"
        " fakeout metrics!"
    )
  except Exception as e:
    print(f"Error updating Google Sheet: {e}")
else:
  print("⚠️ No trade logs found.")
