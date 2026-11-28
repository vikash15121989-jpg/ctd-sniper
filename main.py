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
    "=== CTD_SNIPER BACKTEST: RANGE POC (SW_HIGH -> SW_LOW) + BREAK + SWIPE"
    " + POC ==="
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


def compute_range_poc(sub_df, price_low=None, price_high=None, bins=50):
  """
    Computes Volume Profile Point of Control (POC) over a specific range
    (like SW_High -> SW_Low range box in chart), using typical price `(H+L+C)/3`
    weighted by `Volume`.
    """
  if sub_df is None or len(sub_df) < 3:
    return None

  low_min = (
      float(price_low) if price_low is not None else float(sub_df["Low"].min())
  )
  high_max = (
      float(price_high)
      if price_high is not None
      else float(sub_df["High"].max())
  )
  if high_max <= low_min:
    return None

  tp = ((sub_df["High"] + sub_df["Low"] + sub_df["Close"]) / 3.0).values
  vol = sub_df["Volume"].values

  edges = np.linspace(low_min, high_max, bins + 1)
  bin_vols = np.zeros(bins, dtype=float)

  idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
  for b_i, v_val in zip(idx, vol):
    if not np.isnan(v_val) and v_val > 0:
      bin_vols[b_i] += float(v_val)

  if bin_vols.sum() <= 0:
    return None

  best_bin = int(np.argmax(bin_vols))
  return float(0.5 * (edges[best_bin] + edges[best_bin + 1]))


def get_daily_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish reversal candle signal."""
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
    1) Monthly Swing High (`sw_high`)
    2) Subsequent Monthly Swing Low (`sw_low`)
    3) Swing High break (`break_idx`)
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

    subsequent_lows = [sl for sl in swing_lows if sl["idx"] > h_idx]
    if not subsequent_lows:
      continue
    sl_item = subsequent_lows[0]
    l_idx = sl_item["idx"]
    sw_low = sl_item["price"]

    if sw_low >= sw_high * 0.98:
      continue

    break_idx = None
    for k in range(h_idx + 1, n):
      if float(closes[k]) > sw_high or float(highs[k]) > sw_high * 1.005:
        break_idx = k
        break

    if break_idx is not None:
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


def backtest_range_poc_after_swipe(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    swipe_tol_pct=0.5,
    poc_tol_pct=2.0,
    first_trade_only=True,
):
  """
    Computes POC inside `[sw_high_date` to `sw_low_date]` range (bounded by `[sw_low, sw_high]`).
    After `sw_high` breaks and `sw_low` liquidity swipe happens, enters when price is
    near `POC` (`<= poc_tol_pct`) with bullish reversal signal & above 200 EMA.
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

    for st in setups:
      sw_high = st["sw_high"]
      sw_low = st["sw_low"]

      # Range slice from SW_High month start through SW_Low month end
      range_df = df_d[
          (df_d.index >= st["sw_high_date"])
          & (df_d.index < (st["sw_low_date"] + pd.offsets.MonthEnd(1)))
      ]
      poc_val = compute_range_poc(
          range_df, price_low=sw_low, price_high=sw_high, bins=50
      )
      if poc_val is None or poc_val <= 0:
        continue

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
      swipe_seen = False
      swipe_bar_idx = None

      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      signal_name = ""
      poc_dist_pct = 0.0

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
                "Range_POC": round(poc_val, 2),
                "POC_Dist_%": round(poc_dist_pct, 2),
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
                "Range_POC": round(poc_val, 2),
                "POC_Dist_%": round(poc_dist_pct, 2),
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

        if first_trade_only and taken_for_setup:
          break

        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue

        # Step 1: Liquidity swipe near SW_Low
        swept_low = (d_low <= swipe_trigger_lvl) and (d_close >= reclaim_min_lvl)
        if swept_low:
          swipe_seen = True
          swipe_bar_idx = k

        # Step 2: After swipe, check if price is near the Range POC + bullish candle + above 200 EMA
        if swipe_seen and (k - swipe_bar_idx) <= 12 and d_close > d_ema200:
          dist_from_poc = abs(d_close - poc_val) / poc_val * 100.0
          near_poc = (dist_from_poc <= poc_tol_pct) or (
              d_low <= poc_val * (1.0 + poc_tol_pct / 100.0)
              and d_high >= poc_val * (1.0 - poc_tol_pct / 100.0)
          )

          if near_poc:
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
              cand_sl = min(d_low, sw_low) * 0.99
              risk_pts = d_close - cand_sl
              if risk_pts <= 0:
                continue

              taken_for_setup = True
              signal_name = bull_sig
              poc_dist_pct = dist_from_poc
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
  t = backtest_range_poc_after_swipe(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      swipe_tol_pct=0.5,
      poc_tol_pct=2.0,
      first_trade_only=True,
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
  print(" BACKTEST: RANGE POC + SW HIGH BREAK + SW LOW SWIPE")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "M_RANGE_POC_SWIPE_BACKTEST_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 15)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: RANGE POC + SW HIGH BREAK + SW LOW SWIPE |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "SW High",
        "SW Low",
        "Range POC",
        "POC Dist %",
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
    
