from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER BASE DEFENSE + SWING BREAKOUT BACKTEST (NO LOOKAHEAD) ===")

LOOKBACK_SWING = 3
BASE_DEFENSE_TOLERANCE = 0.02  # Allow at most 2% wick/close tolerance below Base Low
MIN_BREAK_BUFFER_PCT = 0.01  # Close >= Swing_High * 1.01
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
# 2. DETECT SWING HIGH + DEFENDED BASE FLOOR BEFORE `sig_idx`
# -------------------------------------------------------------
def check_base_defense_swing_breakout(highs, lows, closes, sig_idx):
  """
  Looks for:
  1. A swing high `swing_val` at index `s_idx` (before `sig_idx`)
  2. A base support `base_low` prior to/around the swing formation that price
     does NOT break down below (`closes[k] >= base_low * (1 - tol)`)
  3. Pullback/consolidation after `s_idx` where price holds above `base_low`
     without breaking `swing_val` earlier
  4. Signal month `sig_idx` closes >= `swing_val * (1 + MIN_BREAK_BUFFER_PCT)`
  """
  if sig_idx < LOOKBACK_SWING * 2 + 3:
    return False, None, None

  # Search recent swing high `s_idx` (at least 2 months before `sig_idx` so pullback/base test happens)
  for s_idx in range(sig_idx - 2, LOOKBACK_SWING, -1):
    swing_val = float(highs[s_idx])
    if swing_val <= 0:
      continue

    # Check local swing high around `s_idx`
    left_ok = all(swing_val >= highs[k] for k in range(max(0, s_idx - LOOKBACK_SWING), s_idx))
    right_ok = all(
        swing_val >= highs[k] for k in range(s_idx + 1, min(sig_idx, s_idx + LOOKBACK_SWING + 1))
    )
    if not (left_ok and right_ok):
      continue

    # Ensure `swing_val` was not already broken before `sig_idx`
    if any(highs[k] > swing_val for k in range(s_idx + 1, sig_idx)):
      continue

    # Base floor from window leading into and through the post-swing pullback
    base_start = max(0, s_idx - 6)
    base_low = float(np.min(lows[base_start:sig_idx]))
    if base_low <= 0 or swing_val <= base_low:
      continue

    # Base width shouldn't be an extreme multi-bagger blow-off box (e.g., <= 120%)
    base_span_pct = ((swing_val - base_low) / base_low) * 100.0
    if base_span_pct > 120.0:
      continue

    # Price must NOT break down below base support (price "niche nahi jaane de raha")
    floor_limit = base_low * (1.0 - BASE_DEFENSE_TOLERANCE)
    if np.any(closes[base_start : sig_idx + 1] < floor_limit):
      continue

    # Post-swing pullback must have occurred (at least one month after swing_val closed below swing_val)
    post_swing_closes = closes[s_idx + 1 : sig_idx]
    if len(post_swing_closes) == 0 or not np.any(post_swing_closes < swing_val):
      continue

    # Signal month `sig_idx` breaks out cleanly above `swing_val` on close
    sig_close = float(closes[sig_idx])
    if sig_close >= swing_val * (1.0 + MIN_BREAK_BUFFER_PCT):
      return True, round(swing_val, 2), round(base_low, 2)

  return False, None, None


# -------------------------------------------------------------
# 3. BACKTEST ENGINE (SIGNAL AT `i-1` CLOSE -> ENTRY AT `i` OPEN)
# -------------------------------------------------------------
def backtest_base_defense_breakout(symbol):
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

    if df is None or df.empty or len(df) < 15:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < 15:
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
    pending_setup = None  # (swing_high, base_low, sig_month_str)
    last_entry_idx = -99

    for i in range(LOOKBACK_SWING * 2 + 4, n):
      current_date = df.index[i]
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
                "Base_Low": b_low,
                "Swing_High": s_high,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_setup is not None:
        s_high, b_low, sig_month_str = pending_setup
        pending_setup = None

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
                  "Base_Low": b_low,
                  "Swing_High": s_high,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= 3:
        ok, s_high, b_low = check_base_defense_swing_breakout(
            highs, lows, closes, i
        )
        if ok:
          pending_setup = (s_high, b_low, df.index[i].strftime("%b %Y"))

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 4. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_base_defense_breakout(sym)
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
  print(f" BASE DEFENSE + SWING BREAKOUT RESULTS (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "BASE_DEFENSE_SWING_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 12)
    )
    ws.clear()
    ws.append_row([
        "BASE DEFENSE + SWING BREAKOUT LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
