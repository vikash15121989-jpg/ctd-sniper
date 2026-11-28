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
    "=== CTD_SNIPER BACKTEST: MONTHLY SWING HIGH BREAK + SWING LOW LIQUIDITY"
    " SWIPE ==="
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
  """Returns bullish reversal candle signal after liquidity swipe."""
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
      and (body / total_range) >= 0.40
      and ((curr_c - curr_l) / total_range) >= 0.60
  ):
    return "BULLISH_GREEN"

  return ""


def find_high_low_break_setups(df_m, lookback=2, max_age_days=1095):
  """
    Finds monthly structures:
    1) Monthly Swing High at index `h_idx` (`sw_high`)
    2) Subsequent Monthly Swing Low at index `l_idx` (`sw_low`, `l_idx > h_idx`)
    3) Swing High break (`break_idx > l_idx` or after `h_idx`)
    Returns list of valid structures with marked `sw_high`, `sw_low`, and `break_date`.
    """
  highs = df_m["High"].values
  lows = df_m["Low"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 3):
    return []

  last_dt = dates[-1]
  swing_highs = []
  swing_lows = []

  for j in range(lookback, n - lookback):
    if (last_dt - dates[j]).days > max_age_days:
      continue

    hv = float(highs[j])
    if all(hv > float(highs[k]) for k in range(j - lookback, j)) and all(
        hv > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    ):
      swing_highs.append({"idx": j, "date": dates[j], "price": hv})

    lv = float(lows[j])
    if all(lv < float(lows[k]) for k in range(j - lookback, j)) and all(
        lv < float(lows[k]) for k in range(j + 1, j + lookback + 1)
    ):
      swing_lows.append({"idx": j, "date": dates[j], "price": lv})

  setups = []
  for sh_item in swing_highs:
    h_idx = sh_item["idx"]
    sw_high = sh_item["price"]

    # Find first swing low AFTER this swing high
    subsequent_lows = [sl for sl in swing_lows if sl["idx"] > h_idx]
    if not subsequent_lows:
      continue
    sl_item = subsequent_lows[0]
    l_idx = sl_item["idx"]
    sw_low = sl_item["price"]

    # Ensure sw_low is meaningfully below sw_high
    if sw_low >= sw_high * 0.98:
      continue

    # Check if sw_high is broken at any month after h_idx
    break_idx = None
    for k in range(h_idx + 1, n):
      if float(closes[k]) > sw_high or float(highs[k]) > sw_high * 1.005:
        break_idx = k
        break

    if break_idx is not None:
      # Start scanning for liquidity swipe after both swing low and break have occurred
      start_scan_idx = max(l_idx, break_idx)
      if start_scan_idx < n:
        setups.append({
            "sw_high_date": sh_item["date"],
            "sw_high": sw_high,
            "sw_low_date": sl_item["date"],
            "sw_low": sw_low,
            "break_date": dates[break_idx],
            "scan_start_date": dates[start_scan_idx],
        })

  return setups


def backtest_liquidity_swipe(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    swipe_tol_pct=0.5,
    first_swipe_only=True,
):
  """
    After monthly swing high is marked, swing low is marked, and swing high breaks:
    Checks daily bars for a liquidity swipe of `sw_low` (low <= sw_low * (1 + swipe_tol_pct/100),
    reclaiming close >= sw_low * 0.99) + bullish signal + 200 EMA / turnover filter.
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

    setups = find_high_low_break_setups(df_m, lookback=2, max_age_days=1095)
    if not setups:
      return trades

    swipe_trigger_lvl = None
    for st in setups:
      sw_high = st["sw_high"]
      sw_low = st["sw_low"]
      swipe_trigger_lvl = sw_low * (1.0 + swipe_tol_pct / 100.0)
      reclaim_min_lvl = sw_low * 0.99

      sub = df_d[df_d.index >= st["scan_start_date"]]
      if len(sub) < 3:
        continue

      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      avg_to = sub["Avg_Turnover_20D"].values
      dates = sub.index

      in_trade = False
      taken_for_setup = False
      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      signal_name = ""
      sweep_depth_pct = 0.0

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
                "SW_High": round(sw_high, 2),
                "SW_Low": round(sw_low, 2),
                "Sweep_Depth_%": round(sweep_depth_pct, 2),
                "Break_Month": st["break_date"].strftime("%Y-%m"),
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
                "SW_High": round(sw_high, 2),
                "SW_Low": round(sw_low, 2),
                "Sweep_Depth_%": round(sweep_depth_pct, 2),
                "Break_Month": st["break_date"].strftime("%Y-%m"),
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

        if first_swipe_only and taken_for_setup:
          break

        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue

        # Liquidity swipe condition: price sweeps at/below sw_low zone and reclaims above sw_low*0.99
        swept_low = (d_low <= swipe_trigger_lvl) and (d_close >= reclaim_min_lvl)
        above_ema = d_close > d_ema200

        if swept_low and above_ema:
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
            cand_sl = d_low * 0.99
            risk_pts = d_close - cand_sl
            if risk_pts <= 0:
              continue

            taken_for_setup = True
            signal_name = bull_sig
            sweep_depth_pct = ((sw_low - d_low) / sw_low) * 100.0
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
  t = backtest_liquidity_swipe(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      swipe_tol_pct=0.5,
      first_swipe_only=True,
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
  print(" BACKTEST: SWING HIGH BREAK + SWING LOW LIQUIDITY SWIPE")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "M_SWING_HIGH_BREAK_LOW_SWIPE_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 14)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: MONTHLY SWING HIGH BREAK + SWING LOW LIQUIDITY SWIPE |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "SW High",
        "SW Low",
        "Sweep Depth %",
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
    
