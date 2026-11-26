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
    "=== CTD_SNIPER SWING BREAK + RUN-UP + CORRECTION TO SWING DZ + BULLISH"
    " SIGNAL ==="
)

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


def get_daily_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish signal name if candle at/near flipped swing high is bullish."""
  total_range = curr_h - curr_l
  if total_range <= 1e-9:
    return ""

  body = abs(curr_c - curr_o)
  lower_wick = min(curr_o, curr_c) - curr_l
  upper_wick = curr_h - max(curr_o, curr_c)
  is_green = curr_c > curr_o

  # 1. Bullish Engulfing
  if (
      prev_c < prev_o
      and is_green
      and min(curr_o, curr_c) <= min(prev_o, prev_c)
      and max(curr_o, curr_c) >= max(prev_o, prev_c)
  ):
    return "BULLISH_ENGULFING"

  # 2. Hammer / Strong Lower Wick Rejection
  if (
      curr_c >= curr_o
      and body > 1e-9
      and lower_wick >= 1.8 * body
      and upper_wick <= 0.4 * body
  ):
    return "HAMMER"

  # 3. Clean Bullish Green Candle
  if (
      is_green
      and (body / total_range) >= 0.35
      and ((curr_c - curr_l) / total_range) >= 0.55
  ):
    return "BULLISH_GREEN"

  return ""


def get_first_break_monthly_swings(df_m, lookback=3, max_age_days=730):
  """Finds recent monthly swing highs and records ONLY their FIRST breakout month."""
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return []

  last_dt = dates[-1]
  swings = []
  consumed_swing_indices = set()

  for j in range(lookback, n - lookback):
    if j in consumed_swing_indices:
      continue
    if (last_dt - dates[j]).days > max_age_days:
      continue

    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if not (left_ok and right_ok):
      continue

    for k in range(j + 1, n):
      if float(closes[k]) > val:
        consumed_swing_indices.add(j)
        next_break_dt = dates[k + 1] if (k + 1) < n else None
        swings.append({
            "swing_idx": j,
            "swing_date": dates[j],
            "level": val,
            "break_month_start": dates[k],
            "window_end": next_break_dt,
        })
        break

  return swings[-2:]


def backtest_swing_run_correction(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    zone_tol_pct=1.5,
):
  """
    1. Monthly swing high `sw_lvl` breaks.
    2. Price must run up at least `min_run_pct` above `sw_lvl` (not stuck right on breakout).
    3. Price corrects back to `sw_lvl` (+/- `zone_tol_pct`) above 200 EMA.
    4. Enters on daily bullish candle signal.
    """
  trades = []
  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_m = yf.download(
        ticker,
        start="2018-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    df_d = yf.download(
        ticker,
        start="2018-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return trades
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 205:
      return trades

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    swings = get_first_break_monthly_swings(
        df_m, lookback=3, max_age_days=730
    )
    used_swings = set()

    for sw in swings:
      sw_lvl = sw["level"]
      swing_key = (sw["swing_date"].strftime("%Y-%m"), round(sw_lvl, 2))
      if swing_key in used_swings:
        continue

      start_dt = sw["break_month_start"]
      end_dt = sw["window_end"]
      if end_dt is not None:
        sub = df_d[(df_d.index >= start_dt) & (df_d.index < end_dt)]
      else:
        sub = df_d[df_d.index >= start_dt]

      if len(sub) < 4:
        continue

      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      avg_to = sub["Avg_Turnover_20D"].values
      dates = sub.index

      run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
      dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
      dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)

      ran_up_after_break = False
      max_run_pct_seen = 0.0
      in_trade = False
      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      signal_name = ""
      recorded_run_pct = 0.0

      for k in range(1, len(sub)):
        d_open = float(opens[k])
        d_high = float(highs[k])
        d_low = float(lows[k])
        d_close = float(closes[k])
        d_ema200 = float(ema200[k])
        d_avg_to = float(avg_to[k])
        curr_dt = dates[k]

        if in_trade:
          if d_low <= sl_price:
            pnl = ((sl_price - entry_price) / entry_price) * 100.0
            trades.append({
                "Symbol": symbol,
                "Signal": signal_name,
                "Run_Up_%": round(recorded_run_pct, 2),
                "Swing_DZ": round(sw_lvl, 2),
                "Break_Month": sw["break_month_start"].strftime("%Y-%m"),
                "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                "Entry_Price": round(entry_price, 2),
                "SL_Price": round(sl_price, 2),
                "TP_Price": round(tp_price, 2),
                "Result": "LOSS",
                "PnL_%": round(pnl, 2),
            })
            in_trade = False
            break

          if d_high >= tp_price:
            pnl = ((tp_price - entry_price) / entry_price) * 100.0
            trades.append({
                "Symbol": symbol,
                "Signal": signal_name,
                "Run_Up_%": round(recorded_run_pct, 2),
                "Swing_DZ": round(sw_lvl, 2),
                "Break_Month": sw["break_month_start"].strftime("%Y-%m"),
                "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                "Entry_Price": round(entry_price, 2),
                "SL_Price": round(sl_price, 2),
                "TP_Price": round(tp_price, 2),
                "Result": "WIN",
                "PnL_%": round(pnl, 2),
            })
            in_trade = False
            break
          continue

        # Track how far price ran above the broken monthly swing high
        curr_run_pct = ((d_high - sw_lvl) / sw_lvl) * 100.0
        if curr_run_pct > max_run_pct_seen:
          max_run_pct_seen = curr_run_pct
        if d_high >= run_target_lvl:
          ran_up_after_break = True

        # Must run up first, then correct back near/into the flipped swing level
        if not ran_up_after_break:
          continue
        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue

        above_200_ema = d_close > d_ema200
        back_at_swing_dz = (d_low <= dz_high) and (d_high >= dz_low)

        if above_200_ema and back_at_swing_dz:
          bull_sig = get_daily_bullish_signal(
              float(opens[k - 1]),
              float(highs[k - 1]),
              float(lows[k - 1]),
              float(closes[k - 1]),
              d_open,
              d_high,
              d_low,
              d_close,
          )
          if bull_sig:
            cand_sl = min(d_low, sw_lvl) * 0.99
            risk_pts = d_close - cand_sl
            if risk_pts <= 0:
              continue

            used_swings.add(swing_key)
            signal_name = bull_sig
            recorded_run_pct = max_run_pct_seen
            entry_price = d_close
            sl_price = cand_sl
            tp_price = entry_price + (rr_ratio * risk_pts)
            entry_dt = curr_dt
            in_trade = True

    return trades
  except Exception:
    return []


# -------------------------------------------------------------
# 2. RUN BACKTEST & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
for sym in STOCKS:
  t = backtest_swing_run_correction(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      zone_tol_pct=1.5,
  )
  if t:
    all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  losses = len(df_res[df_res["Result"] == "LOSS"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print("\n=======================================================")
  print(" SWING BREAK + RUN-UP + CORRECTION TO DZ + BULLISH SIGNAL")
  print("=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("=======================================================")

  tab_name = "M_SWING_RUN_CORRECTION_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 13)
    )
    ws.clear()
    ws.append_row([
        "SWING BREAK + RUN-UP + CORRECTION TO DZ + BULLISH SIGNAL |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "Run Up %",
        "Swing DZ",
        "Break Month",
        "Entry Date",
        "Exit Date",
        "Entry Price",
        "SL Price",
        "TP Price",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
