from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MONTHLY SWING + COMPACT RANGE BREAKOUT ===")

# Strategy Parameters
LOOKBACK_SWING = 3
MIN_BREAK_BUFFER_PCT = 0.01
MAX_BREAK_EXTENSION_PCT = 0.12  # Breakout candle zyada bhagi na ho (Close vs Swing_High)
BASE_BREAK_TOLERANCE = 0.02
STOP_LOSS_PCT = 0.06
TAKE_PROFIT_PCT = 0.12
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
# 2. MONTHLY SWING + COMPACT BREAKOUT SIGNAL CHECK
# -------------------------------------------------------------
def find_compact_monthly_breakout(highs, lows, closes, sig_idx):
  if sig_idx < LOOKBACK_SWING * 2 + 4:
    return False, None, None, None

  c_high = float(highs[sig_idx])
  c_low = float(lows[sig_idx])
  c_close = float(closes[sig_idx])
  brk_range = c_high - c_low

  if brk_range <= 0:
    return False, None, None, None

  for s_idx in range(sig_idx - 2, LOOKBACK_SWING, -1):
    swing_val = float(highs[s_idx])
    if swing_val <= 0:
      continue

    # Clean swing high check
    left_ok = all(
        swing_val >= highs[k]
        for k in range(max(0, s_idx - LOOKBACK_SWING), s_idx)
    )
    right_ok = all(
        swing_val >= highs[k]
        for k in range(s_idx + 1, min(sig_idx, s_idx + LOOKBACK_SWING + 1))
    )
    if not (left_ok and right_ok):
      continue

    # Swing high was not already broken before sig_idx
    if any(highs[k] > swing_val for k in range(s_idx + 1, sig_idx)):
      continue

    # Correction/pullback phase between s_idx + 1 and sig_idx - 1
    corr_highs = highs[s_idx + 1 : sig_idx]
    corr_lows = lows[s_idx + 1 : sig_idx]
    corr_closes = closes[s_idx + 1 : sig_idx]

    if len(corr_closes) < 1 or not np.any(corr_closes < swing_val):
      continue

    base_floor = float(np.min(corr_lows))
    if base_floor <= 0 or swing_val <= base_floor:
      continue

    # Correction candles' ranges
    corr_ranges = corr_highs - corr_lows
    if len(corr_ranges) == 0:
      continue
    max_corr_range = float(np.max(corr_ranges))

    # Breakout candle range correction ke kisi bhi candle se badi na ho
    if brk_range > max_corr_range:
      continue

    # Breakout confirmation + zyada bhaga hua na ho
    if c_close < swing_val * (1.0 + MIN_BREAK_BUFFER_PCT):
      continue
    if c_close > swing_val * (1.0 + MAX_BREAK_EXTENSION_PCT):
      continue

    return (
        True,
        round(swing_val, 2),
        round(base_floor, 2),
        round((brk_range / max_corr_range), 2),
    )

  return False, None, None, None


# -------------------------------------------------------------
# 3. BACKTEST ENGINE (NO LOOKAHEAD, CONSERVATIVE SL CHECK)
# -------------------------------------------------------------
def backtest_compact_breakout(symbol):
  trades = []
  try:
    clean_sym = symbol.replace(".NS", "").strip().upper()
    ticker_symbol = f"{clean_sym}.NS"

    df = yf.download(
        ticker_symbol,
        start="2015-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    if df is None or df.empty or len(df) < 14:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)
    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < 14:
      return trades

    idx = pd.to_datetime(df.index).tz_localize(None)
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
    pending_trade = None
    last_entry_idx = -99

    for i in range(LOOKBACK_SWING * 2 + 4, n):
      current_date = idx[i]
      curr_open = float(opens[i])
      curr_high = float(highs[i])
      curr_low = float(lows[i])

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          if curr_low <= stop_loss and curr_high >= take_profit:
            is_win = False
          else:
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
                "Base_Floor": b_floor,
                "Swing_High": s_high,
                "Range_Ratio": r_ratio,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_trade is not None:
        s_high, b_floor, r_ratio, sig_month_str = pending_trade
        pending_trade = None

        if curr_open > 0 and (i - last_entry_idx) >= 3:
          entry_price = curr_open
          stop_loss = entry_price * (1.0 - STOP_LOSS_PCT)
          take_profit = entry_price * (1.0 + TAKE_PROFIT_PCT)
          entry_date = current_date
          max_price_reached = curr_high
          in_trade = True
          last_entry_idx = i

          if curr_low <= stop_loss or curr_high >= take_profit:
            if curr_low <= stop_loss and curr_high >= take_profit:
              is_win = False
            else:
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
                  "Base_Floor": b_floor,
                  "Swing_High": s_high,
                  "Range_Ratio": r_ratio,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= 3:
        ok, s_high, b_floor, r_ratio = find_compact_monthly_breakout(
            highs, lows, closes, i
        )
        if ok:
          pending_trade = (
              s_high,
              b_floor,
              r_ratio,
              idx[i].strftime("%b %Y"),
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
    t = backtest_compact_breakout(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  df_res.replace([np.inf, -np.inf], np.nan, inplace=True)
  df_res = df_res.fillna("")

  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print(f"\n=======================================================")
  print(f" MONTHLY SWING + COMPACT BREAKOUT (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "MONTHLY_COMPACT_BREAKOUT_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 12)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY SWING + COMPACT BREAKOUT LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
    
