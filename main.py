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
    "=== CTD_SNIPER BACKTEST: REFINED MONTHLY SWING BREAKOUT (WIN-RATE"
    " BOOST) ==="
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


def get_clean_bullish_signal(
    prev_o,
    prev_h,
    prev_l,
    prev_c,
    curr_o,
    curr_h,
    curr_l,
    curr_c,
    min_range_pct=1.5,
):
  """
    Returns strong bullish reversal signal while filtering out
    tiny flat/doji candles (`total_range / curr_c >= min_range_pct / 100`).
    """
  if curr_c <= 0:
    return ""
  total_range = curr_h - curr_l
  if total_range <= 1e-9 or ((total_range / curr_c) * 100.0) < min_range_pct:
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
      and (body / curr_c) >= 0.004
      and lower_wick >= 2.0 * body
      and upper_wick <= 0.35 * body
  ):
    return "HAMMER"

  # 3. Strong Bullish Green Candle
  if (
      is_green
      and (body / total_range) >= 0.50
      and ((curr_c - curr_l) / total_range) >= 0.75
      and curr_c > prev_h
  ):
    return "STRONG_BULLISH_GREEN"

  return ""


def get_broken_monthly_swings(df_m, lookback=3, max_age_days=1095):
  """Finds monthly swing highs broken by a subsequent monthly close."""
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return []

  last_dt = dates[-1]
  swings = []

  for j in range(lookback, n - lookback):
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
        swings.append({
            "swing_date": dates[j],
            "level": val,
            "break_month_start": dates[k],
        })
        break

  return swings


def backtest_refined_swing_breakout(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    min_run_pct=5.0,
    zone_tol_pct=1.5,
    min_range_pct=1.5,
    min_break_vol_mult=1.5,
    first_pullback_only=True,
):
  """
    Refined version of the high-performing monthly swing breakout pullback setup:
    - Monthly swing high broken + >= min_run_pct rally
    - Breakout/post-break volume surge >= min_break_vol_mult * 20D avg volume
    - Pullback into flipped Swing_DZ (`+- zone_tol_pct`) above 200 EMA
    - Clean bullish reversal candle (`>= min_range_pct` range)
    - First pullback only per broken swing
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
    df_d["Avg_Vol_20D"] = df_d["Volume"].rolling(window=20).mean()

    swings = get_broken_monthly_swings(df_m, lookback=3, max_age_days=1095)
    if not swings:
      return trades

    for sw in swings:
      sw_lvl = sw["level"]
      run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
      dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
      dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)

      sub = df_d[df_d.index >= sw["break_month_start"]]
      if len(sub) < 4:
        continue

      # Check if breakout/post-break move had strong volume surge (>= min_break_vol_mult * 20D avg vol)
      vol_ratios = (
          sub["Volume"].values / np.where(sub["Avg_Vol_20D"].values > 0, sub["Avg_Vol_20D"].values, np.nan)
      )
      max_vol_mult = float(np.nanmax(vol_ratios)) if not np.all(np.isnan(vol_ratios)) else 0.0
      if max_vol_mult < min_break_vol_mult:
        continue

      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      avg_to = sub["Avg_Turnover_20D"].values
      dates = sub.index

      in_trade = False
      taken_for_swing = False
      running_max_high = float(highs[0])

      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      signal_name = ""
      run_up_at_entry = 0.0

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
                "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
                "Swing_DZ": round(sw_lvl, 2),
                "Run_Up_%": round(run_up_at_entry, 2),
                "Max_Vol_Mult": round(max_vol_mult, 2),
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
                "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
                "Swing_DZ": round(sw_lvl, 2),
                "Run_Up_%": round(run_up_at_entry, 2),
                "Max_Vol_Mult": round(max_vol_mult, 2),
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

        if first_pullback_only and taken_for_swing:
          break

        prior_max = running_max_high
        running_max_high = max(running_max_high, d_high)

        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue
        if d_close <= d_ema200:
          continue

        # Must have run up >= min_run_pct and now be correcting back toward flipped Swing_DZ
        if prior_max < run_target_lvl or d_close >= prior_max:
          continue

        in_or_near_dz = (d_low <= dz_high) and (d_close >= dz_low)
        if in_or_near_dz:
          bull_sig = get_clean_bullish_signal(
              float(opens[k - 1]),
              float(highs[k - 1]),
              float(lows[k - 1]),
              float(closes[k - 1]),
              d_open,
              d_high,
              d_low,
              d_close,
              min_range_pct=min_range_pct,
          )
          if bull_sig:
            cand_sl = min(d_low, sw_lvl) * 0.99
            risk_pts = d_close - cand_sl
            if risk_pts <= 0 or ((risk_pts / d_close) * 100.0) > 6.0:
              continue

            taken_for_swing = True
            signal_name = bull_sig
            run_up_at_entry = ((prior_max - sw_lvl) / sw_lvl) * 100.0
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
  t = backtest_refined_swing_breakout(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      min_run_pct=5.0,
      zone_tol_pct=1.5,
      min_range_pct=1.5,
      min_break_vol_mult=1.5,
      first_pullback_only=True,
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

  print("\n===========================================================")
  print(" BACKTEST: REFINED MONTHLY SWING BREAKOUT PULLBACK")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "M_SWING_BREAKOUT_REFINED_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 14)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: REFINED MONTHLY SWING BREAKOUT PULLBACK |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "Swing Date",
        "Swing DZ",
        "Run Up %",
        "Max Vol Mult",
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
    
