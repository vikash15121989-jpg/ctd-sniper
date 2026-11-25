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
    "=== CTD_SNIPER MONTHLY SWING + DAILY PULLBACK FOOTPRINT + BREAKOUT ==="
)

# Strategy & Footprint Parameters
LOOKBACK_SWING = 3
MIN_BREAK_BUFFER_PCT = 0.01
BASE_BREAK_TOLERANCE = 0.02
MIN_DAILY_VOL_RATIO = 1.15  # Daily up-day vol / down-day vol during pullback
MIN_AVG_DAILY_CLV = 0.48  # Avg daily close position in range during pullback
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
# 2. DAILY FOOTPRINT CHECK INSIDE PULLBACK WINDOW
# -------------------------------------------------------------
def check_daily_pullback_footprint(daily_df, start_dt, end_dt, base_floor):
  """
  Inspects daily candles strictly between `start_dt` and `end_dt` (no lookahead
  past `end_dt`, i.e., end of pullback month before breakout month):
  - Checks no daily close breakdown below `base_floor * (1 - BASE_BREAK_TOLERANCE)`
  - Computes daily Up-Day Volume / Down-Day Volume ratio
  - Computes average daily CLV to see dip absorption / rejection of lows
  """
  if daily_df is None or daily_df.empty:
    return False, None, None

  sub = daily_df.loc[
      (daily_df.index >= start_dt) & (daily_df.index <= end_dt)
  ]
  if len(sub) < 10:
    return False, None, None

  d_opens = sub["Open"].values.astype(float)
  d_highs = sub["High"].values.astype(float)
  d_lows = sub["Low"].values.astype(float)
  d_closes = sub["Close"].values.astype(float)
  d_vols = (
      sub["Volume"].values.astype(float)
      if "Volume" in sub.columns
      else np.full(len(sub), np.nan)
  )

  # Ensure daily closes didn't break base floor
  if np.any(d_closes < base_floor * (1.0 - BASE_BREAK_TOLERANCE)):
    return False, None, None

  # Volume accumulation on daily pullback
  valid_v = ~np.isnan(d_vols) & (d_vols > 0)
  if np.sum(valid_v) < 8:
    return False, None, None

  vo = d_opens[valid_v]
  vc = d_closes[valid_v]
  vv = d_vols[valid_v]

  up_vol = float(np.sum(vv[vc >= vo]))
  down_vol = float(np.sum(vv[vc < vo]))
  if down_vol <= 0:
    return False, None, None

  vol_ratio = up_vol / down_vol
  if vol_ratio < MIN_DAILY_VOL_RATIO:
    return False, None, None

  # Average daily CLV (dip absorption check)
  spans = d_highs - d_lows
  valid_span = spans > 0
  if np.sum(valid_span) < 5:
    return False, None, None

  clvs = (d_closes[valid_span] - d_lows[valid_span]) / spans[valid_span]
  avg_clv = float(np.mean(clvs))
  if avg_clv < MIN_AVG_DAILY_CLV:
    return False, None, None

  return True, round(vol_ratio, 2), round(avg_clv, 2)


# -------------------------------------------------------------
# 3. FIND MONTHLY SWING HIGH + PULLBACK + BREAKOUT SIGNAL
# -------------------------------------------------------------
def find_monthly_swing_pullback_breakout(
    monthly_df, daily_df, highs, lows, closes, sig_idx
):
  if sig_idx < LOOKBACK_SWING * 2 + 4:
    return False, None, None, None, None

  c_close = float(closes[sig_idx])

  for s_idx in range(sig_idx - 2, LOOKBACK_SWING, -1):
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

    # Must have pullback below `swing_val` after `s_idx`
    post_closes = closes[s_idx + 1 : sig_idx]
    post_lows = lows[s_idx + 1 : sig_idx]
    if len(post_closes) < 1 or not np.any(post_closes < swing_val):
      continue

    base_floor = float(np.min(post_lows))
    if base_floor <= 0 or swing_val <= base_floor:
      continue

    if ((swing_val - base_floor) / base_floor) * 100.0 > 85.0:
      continue

    # Breakout confirmation on completed month `sig_idx`
    if c_close < swing_val * (1.0 + MIN_BREAK_BUFFER_PCT):
      continue

    # Pullback date window: from start of month `s_idx + 1` to end of month `sig_idx - 1`
    pb_start_dt = monthly_df.index[s_idx + 1]
    pb_end_month_start = monthly_df.index[sig_idx]
    pb_end_dt = pb_end_month_start - pd.Timedelta(days=1)

    ok_daily, d_vol_ratio, d_avg_clv = check_daily_pullback_footprint(
        daily_df, pb_start_dt, pb_end_dt, base_floor
    )
    if ok_daily:
      return (
          True,
          round(swing_val, 2),
          round(base_floor, 2),
          d_vol_ratio,
          d_avg_clv,
      )

  return False, None, None, None, None


# -------------------------------------------------------------
# 4. BACKTEST ENGINE (NO LOOKAHEAD, CONSERVATIVE SL CHECK)
# -------------------------------------------------------------
def backtest_monthly_daily_pullback(symbol):
  trades = []
  try:
    clean_sym = symbol.replace(".NS", "").strip().upper()
    ticker_symbol = f"{clean_sym}.NS"

    m_df = yf.download(
        ticker_symbol,
        start="2015-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    if m_df is None or m_df.empty or len(m_df) < 14:
      return trades

    if isinstance(m_df.columns, pd.MultiIndex):
      m_df.columns = m_df.columns.get_level_values(0)
    m_df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(m_df) < 14:
      return trades

    d_df = yf.download(
        ticker_symbol,
        start="2015-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if d_df is None or d_df.empty:
      return trades

    if isinstance(d_df.columns, pd.MultiIndex):
      d_df.columns = d_df.columns.get_level_values(0)
    d_df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if d_df.empty:
      return trades

    # Normalize timezone-naive timestamps for safe slicing
    m_idx = pd.to_datetime(m_df.index).tz_localize(None)
    m_df = m_df.copy()
    m_df.index = m_idx

    d_idx = pd.to_datetime(d_df.index).tz_localize(None)
    d_df = d_df.copy()
    d_df.index = d_idx

    opens = m_df["Open"].values.astype(float)
    highs = m_df["High"].values.astype(float)
    lows = m_df["Low"].values.astype(float)
    closes = m_df["Close"].values.astype(float)
    n = len(m_df)

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    max_price_reached = 0.0
    pending_trade = None
    last_entry_idx = -99

    for i in range(LOOKBACK_SWING * 2 + 4, n):
      current_date = m_df.index[i]
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
                "Daily_Vol_Ratio": d_vr,
                "Daily_Avg_CLV": d_clv,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_trade is not None:
        s_high, b_floor, d_vr, d_clv, sig_month_str = pending_trade
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
                  "Daily_Vol_Ratio": d_vr,
                  "Daily_Avg_CLV": d_clv,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= 3:
        ok, s_high, b_floor, d_vr, d_clv = find_monthly_swing_pullback_breakout(
            m_df, d_df, highs, lows, closes, i
        )
        if ok:
          pending_trade = (
              s_high,
              b_floor,
              d_vr,
              d_clv,
              m_df.index[i].strftime("%b %Y"),
          )

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 5. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_monthly_daily_pullback(sym)
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
  print(f" MONTHLY SWING + DAILY PULLBACK FOOTPRINT (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "MONTHLY_DAILY_PULLBACK_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 14)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY SWING + DAILY PULLBACK FOOTPRINT LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
    
