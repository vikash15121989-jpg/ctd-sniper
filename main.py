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
    "=== CTD_SNIPER BASE (>=2 TESTS) + BULL MATH (VOL + CLV + 10M SMA)"
    " ==="
)

# Strategy & Bull-Math Parameters
LOOKBACK_SWING = 3
MIN_BASE_TESTS = 2
BASE_TEST_TOLERANCE = 0.03
BASE_BREAK_TOLERANCE = 0.015
MIN_BREAK_BUFFER_PCT = 0.01
MIN_UP_DOWN_VOL_RATIO = 1.25  # Base accumulation: green month vol / red month vol
MIN_CLV = 0.65  # Strong close near candle high on breakout month
SMA_PERIOD = 10  # 10-Month SMA bull regime filter
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
# 2. DETECT SWING -> BASE FLOOR (>=2 TESTS) + BULL MATH
# -------------------------------------------------------------
def check_bull_base_2test_breakout(opens, highs, lows, closes, volumes, sma10, sig_idx):
  """
  1. Swing high `swing_val` at `s_idx` (< sig_idx)
  2. Base floor `base_floor` in `[s_idx + 1, sig_idx - 1]` with >= 2 distinct tests
     and no breakdown close below `base_floor * (1 - BASE_BREAK_TOLERANCE)`
  3. Bull Math:
     - Base accumulation volume: `up_vol / down_vol >= MIN_UP_DOWN_VOL_RATIO`
     - Trend regime: `closes[sig_idx] > sma10[sig_idx]`
     - Breakout candle CLV: `(close - low) / (high - low) >= MIN_CLV`
  4. Breakout close: `closes[sig_idx] >= swing_val * (1 + MIN_BREAK_BUFFER_PCT)`
  """
  min_req = max(LOOKBACK_SWING * 2 + 4, SMA_PERIOD + 2)
  if sig_idx < min_req:
    return False, None, None, 0, None, None

  if np.isnan(sma10[sig_idx]) or sma10[sig_idx] <= 0:
    return False, None, None, 0, None, None

  # Bull trend filter: stock above 10-Month SMA
  if float(closes[sig_idx]) <= float(sma10[sig_idx]):
    return False, None, None, 0, None, None

  # Breakout candle CLV check
  c_high = float(highs[sig_idx])
  c_low = float(lows[sig_idx])
  c_close = float(closes[sig_idx])
  candle_span = c_high - c_low
  if candle_span <= 0:
    return False, None, None, 0, None, None

  clv = (c_close - c_low) / candle_span
  if clv < MIN_CLV:
    return False, None, None, 0, None, None

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

    # Swing high was not already broken before `sig_idx`
    if any(highs[k] > swing_val for k in range(s_idx + 1, sig_idx)):
      continue

    post_lows = lows[s_idx + 1 : sig_idx]
    post_closes = closes[s_idx + 1 : sig_idx]
    if len(post_lows) < 3:
      continue

    base_floor = float(np.min(post_lows))
    if base_floor <= 0 or swing_val <= base_floor:
      continue

    if ((swing_val - base_floor) / base_floor) * 100.0 > 85.0:
      continue

    if np.any(post_closes < base_floor * (1.0 - BASE_BREAK_TOLERANCE)):
      continue
    if c_close < base_floor * (1.0 - BASE_BREAK_TOLERANCE):
      continue

    # Count distinct monthly tests of `base_floor`
    test_zone_high = base_floor * (1.0 + BASE_TEST_TOLERANCE)
    test_indices = []
    for k in range(s_idx + 1, sig_idx):
      if lows[k] <= test_zone_high:
        if not test_indices or (k - test_indices[-1]) >= 2:
          test_indices.append(k)
        else:
          if lows[k] < lows[test_indices[-1]]:
            test_indices[-1] = k

    test_count = len(test_indices)
    if test_count < MIN_BASE_TESTS:
      continue

    # Up-Volume vs Down-Volume ratio across post-swing base window
    base_slice = slice(s_idx + 1, sig_idx)
    b_opens = opens[base_slice]
    b_closes = closes[base_slice]
    b_vols = volumes[base_slice]

    valid_vol_mask = ~np.isnan(b_vols) & (b_vols > 0)
    if np.sum(valid_vol_mask) < 2:
      continue

    v_opens = b_opens[valid_vol_mask]
    v_closes = b_closes[valid_vol_mask]
    v_vols = b_vols[valid_vol_mask]

    up_vol = float(np.sum(v_vols[v_closes >= v_opens]))
    down_vol = float(np.sum(v_vols[v_closes < v_opens]))

    if down_vol <= 0:
      continue

    vol_ratio = up_vol / down_vol
    if vol_ratio < MIN_UP_DOWN_VOL_RATIO:
      continue

    # Breakout close above swing high
    if c_close >= swing_val * (1.0 + MIN_BREAK_BUFFER_PCT):
      return (
          True,
          round(swing_val, 2),
          round(base_floor, 2),
          test_count,
          round(vol_ratio, 2),
          round(clv, 2),
      )

  return False, None, None, 0, None, None


# -------------------------------------------------------------
# 3. BACKTEST ENGINE (SIGNAL AT `i-1` CLOSE -> ENTRY AT `i` OPEN)
# -------------------------------------------------------------
def backtest_bull_base_2test(symbol):
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

    if df is None or df.empty or len(df) < 18:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < 18:
      return trades

    df["SMA10"] = df["Close"].rolling(window=SMA_PERIOD).mean()

    opens = df["Open"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    volumes = (
        df["Volume"].values.astype(float)
        if "Volume" in df.columns
        else np.full(len(df), np.nan)
    )
    sma10 = df["SMA10"].values.astype(float)
    n = len(df)

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    max_price_reached = 0.0
    pending_trade = None
    last_entry_idx = -99

    start_loop = max(LOOKBACK_SWING * 2 + 5, SMA_PERIOD + 3)
    for i in range(start_loop, n):
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
                "Vol_Ratio": v_ratio,
                "CLV": c_clv,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_trade is not None:
        s_high, b_floor, b_tests, v_ratio, c_clv, sig_month_str = pending_trade
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
                  "Vol_Ratio": v_ratio,
                  "CLV": c_clv,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= 3:
        ok, s_high, b_floor, b_tests, v_ratio, c_clv = (
            check_bull_base_2test_breakout(
                opens, highs, lows, closes, volumes, sma10, i
            )
        )
        if ok:
          pending_trade = (
              s_high,
              b_floor,
              b_tests,
              v_ratio,
              c_clv,
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
    t = backtest_bull_base_2test(sym)
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
  print(f" BASE (>=2 TESTS) + BULL MATH RESULTS (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "BASE_2TEST_BULL_MATH_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 14)
    )
    ws.clear()
    ws.append_row([
        "BASE (>=2 TESTS) + BULL MATH LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
    
