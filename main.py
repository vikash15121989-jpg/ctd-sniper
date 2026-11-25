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
    "=== CTD_SNIPER SWING HIGH -> BASE FLOOR (>=2 TESTS) -> BREAKOUT ENTRY ==="
)

# Strategy Parameters
LOOKBACK_SWING = 3
MIN_BASE_TESTS = 2  # Base floor must be tested at least 2 times
BASE_TEST_TOLERANCE = 0.03  # Within 3% above Base Floor counts as a test
BASE_BREAK_TOLERANCE = 0.015  # Close < Base Floor * 0.985 = breakdown (invalid)
MIN_BREAK_BUFFER_PCT = 0.01  # Breakout close >= Swing_High * 1.01
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
# 2. DETECT SWING HIGH -> BASE FLOOR -> >= 2 FLOOR TESTS -> BREAKOUT
# -------------------------------------------------------------
def check_swing_base_2test_breakout(highs, lows, closes, sig_idx):
  """
  For completed signal month `sig_idx`:
  1. Finds an earlier swing high `swing_val` at `s_idx` (< sig_idx).
  2. Finds post-swing / range floor `base_floor` in `[s_idx + 1, sig_idx - 1]`.
  3. Checks that `base_floor` was tested at least `MIN_BASE_TESTS` (2) times
     (separated by at least 1 month so it's distinct tests) without closing
     below `base_floor * (1 - BASE_BREAK_TOLERANCE)`.
  4. Checks that `sig_idx` breaks out on close >= `swing_val * (1 + MIN_BREAK_BUFFER_PCT)`.
  """
  if sig_idx < LOOKBACK_SWING * 2 + 4:
    return False, None, None, 0

  for s_idx in range(sig_idx - 3, LOOKBACK_SWING, -1):
    swing_val = float(highs[s_idx])
    if swing_val <= 0:
      continue

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

    # Swing high must not already be broken before `sig_idx`
    if any(highs[k] > swing_val for k in range(s_idx + 1, sig_idx)):
      continue

    # Post-swing window where floor forms and gets tested
    post_lows = lows[s_idx + 1 : sig_idx]
    post_closes = closes[s_idx + 1 : sig_idx]
    if len(post_lows) < 3:
      continue

    base_floor = float(np.min(post_lows))
    if base_floor <= 0 or swing_val <= base_floor:
      continue

    # Reject overly wide/chaotic boxes (> 85%)
    if ((swing_val - base_floor) / base_floor) * 100.0 > 85.0:
      continue

    # Ensure floor wasn't broken down on close
    if np.any(post_closes < base_floor * (1.0 - BASE_BREAK_TOLERANCE)):
      continue
    if float(closes[sig_idx]) < base_floor * (1.0 - BASE_BREAK_TOLERANCE):
      continue

    # Count distinct monthly tests of `base_floor` (at least 1 month apart)
    test_zone_high = base_floor * (1.0 + BASE_TEST_TOLERANCE)
    test_indices = []
    for k in range(s_idx + 1, sig_idx):
      if lows[k] <= test_zone_high:
        if not test_indices or (k - test_indices[-1]) >= 2:
          test_indices.append(k)
        else:
          # Update if an adjacent month has an even lower test within zone
          if lows[k] < lows[test_indices[-1]]:
            test_indices[-1] = k

    test_count = len(test_indices)
    if test_count < MIN_BASE_TESTS:
      continue

    # Breakout confirmation on completed month `sig_idx`
    if float(closes[sig_idx]) >= swing_val * (1.0 + MIN_BREAK_BUFFER_PCT):
      return True, round(swing_val, 2), round(base_floor, 2), test_count

  return False, None, None, 0


# -------------------------------------------------------------
# 3. BACKTEST ENGINE (SIGNAL AT `i-1` CLOSE -> ENTRY AT `i` OPEN)
# -------------------------------------------------------------
def backtest_swing_base_2test(symbol):
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
    pending_trade = None  # (swing_high, base_floor, tests, sig_month_str)
    last_entry_idx = -99

    for i in range(LOOKBACK_SWING * 2 + 5, n):
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
                "Base_Floor": b_floor,
                "Swing_High": s_high,
                "Base_Tests": b_tests,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_trade is not None:
        s_high, b_floor, b_tests, sig_month_str = pending_trade
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
                  "Base_Tests": b_tests,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= 3:
        ok, s_high, b_floor, b_tests = check_swing_base_2test_breakout(
            highs, lows, closes, i
        )
        if ok:
          pending_trade = (
              s_high,
              b_floor,
              b_tests,
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
    t = backtest_swing_base_2test(sym)
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
  print(f" SWING -> BASE FLOOR (>=2 TESTS) -> BREAKOUT (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "BASE_2TEST_BREAKOUT_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 12)
    )
    ws.clear()
    ws.append_row([
        "SWING -> BASE FLOOR (>=2 TESTS) -> BREAKOUT (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
