from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print(
    "=== CTD_SNIPER SWING HIGH + RANGE + 2% BUFFER (NEXT-MONTH OPEN) ==="
)

# Strategy Parameters
MIN_CONSOLIDATION_MONTHS = 4
MIN_BREAK_BUFFER_PCT = 0.02  # 2% breakout buffer
MAX_CANDLE_RANGE_PCT = 18.0  # Max candle range % filter
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.14
EXCLUDE_KEYWORDS = ("BEES", "ETF", "GOLD", "SILVER", "NIFTY", "SENSEX")

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & CLEAN WATCHLIST LOAD
# -------------------------------------------------------------
try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = []
  for s in raw:
    sym = s.strip().upper()
    if (
        sym
        and len(sym) > 2
        and sym not in ["STOCK", "SYMBOL"]
        and not any(k in sym for k in EXCLUDE_KEYWORDS)
    ):
      STOCKS.append(sym)
  print(f"✅ Loaded {len(STOCKS)} clean stocks from Watchlist.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


# -------------------------------------------------------------
# 2. FIND STRONG UNBROKEN SWING HIGH BEFORE INDEX `end_idx`
# -------------------------------------------------------------
def get_strong_swing_high(highs, end_idx, min_consolidation_months=4):
  start_j = min(end_idx - min_consolidation_months, len(highs) - 1)
  end_j = min_consolidation_months - 1

  for j in range(start_j, end_j, -1):
    is_valid_swing = True
    for k in range(j - min_consolidation_months, j):
      if highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    for k in range(j + 1, j + min_consolidation_months + 1):
      if k < len(highs) and highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    ever_broken = False
    for k in range(j + 1, end_idx):
      if highs[k] > highs[j]:
        ever_broken = True
        break
    if ever_broken:
      continue

    return float(highs[j]), j

  return None, -1


# -------------------------------------------------------------
# 3. BACKTEST: 2% BUFFER + RANGE AT MONTH `i-1` -> ENTRY AT MONTH `i` OPEN
# -------------------------------------------------------------
def backtest_swing_range_2pct_next_open(symbol):
  trades = []
  try:
    clean_sym = symbol.replace(".NS", "").strip().upper()
    ticker_symbol = f"{clean_sym}.NS"

    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < MIN_CONSOLIDATION_MONTHS * 2 + 5:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < MIN_CONSOLIDATION_MONTHS * 2 + 5:
      return trades

    opens = df["Open"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    n = len(df)

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    max_price_reached = 0.0
    used_swing_indices = set()
    pending_entry_swing = None  # (swing_level, swing_idx, signal_month_str)

    for i in range(MIN_CONSOLIDATION_MONTHS * 2 + 3, n):
      current_date = df.index[i]
      curr_open = float(opens[i])
      curr_high = float(highs[i])
      curr_low = float(lows[i])
      curr_close = float(closes[i])

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              ((take_profit - entry_price) / entry_price) * 100.0
              if is_win
              else ((stop_loss - entry_price) / entry_price) * 100.0
          )
          max_profit_pct = (
              ((max_price_reached - entry_price) / entry_price) * 100.0
          )

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": clean_sym,
                "Signal_Month": sig_month_str,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Open": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      # Execute signal confirmed in previous completed month on current month Open
      if pending_entry_swing is not None:
        s_level, s_idx, sig_month_str = pending_entry_swing
        pending_entry_swing = None

        if curr_open > 0 and s_idx not in used_swing_indices:
          entry_price = curr_open
          stop_loss = entry_price * (1.0 - STOP_LOSS_PCT)
          take_profit = entry_price * (1.0 + TAKE_PROFIT_PCT)
          entry_date = current_date
          max_price_reached = curr_high
          in_trade = True
          used_swing_indices.add(s_idx)

          if curr_low <= stop_loss or curr_high >= take_profit:
            is_win = curr_high >= take_profit
            pnl_pct = (
                ((take_profit - entry_price) / entry_price) * 100.0
                if is_win
                else ((stop_loss - entry_price) / entry_price) * 100.0
            )
            max_profit_pct = (
                ((max_price_reached - entry_price) / entry_price) * 100.0
            )

            if entry_date.year in [2025, 2026] or current_date.year in [
                2025,
                2026,
            ]:
              trades.append({
                  "Symbol": clean_sym,
                  "Signal_Month": sig_month_str,
                  "Entry_Date": entry_date.strftime("%b %Y"),
                  "Entry_Open": round(entry_price, 2),
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      # Evaluate completed month `i` for next month `i + 1` Open entry
      if i + 1 < n:
        swing_level, swing_idx = get_strong_swing_high(
            highs, i, MIN_CONSOLIDATION_MONTHS
        )
        if (
            swing_level is not None
            and swing_level > 0
            and swing_idx not in used_swing_indices
        ):
          month_range_pct = (
              ((curr_high - curr_low) / curr_close) * 100.0
              if curr_close > 0
              else 999.0
          )
          trigger_2pct = swing_level * (1.0 + MIN_BREAK_BUFFER_PCT)

          if (
              curr_high >= trigger_2pct
              and curr_close >= swing_level
              and month_range_pct <= MAX_CANDLE_RANGE_PCT
          ):
            pending_entry_swing = (
                swing_level,
                swing_idx,
                df.index[i].strftime("%b %Y"),
            )

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 4. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_swing_range_2pct_next_open(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print(f"\n=======================================================")
  print(f" SWING + RANGE + 2% BUFFER NEXT-OPEN (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "SWING_R2PCT_NEXT_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "SWING + RANGE + 2% BUFFER NEXT-OPEN (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal Month",
        "Entry Date",
        "Entry Open",
        "Max Profit %",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
