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
    "=== CTD_SNIPER TOMORROW WATCHLIST: SWING CROSSED & PULLBACK NEAR"
    " SWING ==="
)

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & STOCK-ONLY WATCHLIST LOAD
# -------------------------------------------------------------
NON_STOCK_KEYWORDS = (
    "BEES",
    "ETF",
    "GOLD",
    "SILVER",
    "NIFTY",
    "LIQUID",
    "SENSEX",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NEXT50",
    "MON100",
    "MAFANG",
)


def is_equity_stock(symbol: str) -> bool:
  s = symbol.strip().upper()
  if not s or len(s) <= 2 or s in {"STOCK", "SYMBOL"}:
    return False
  if s.startswith(("^", "$")):
    return False
  clean_sym = s.replace(".NS", "").replace(".BO", "")
  if any(kw in clean_sym for kw in NON_STOCK_KEYWORDS):
    return False
  return True


try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = list(
      dict.fromkeys([s.strip().upper() for s in raw if is_equity_stock(s)])
  )
  print(
      f"✅ Loaded {len(STOCKS)} equity stocks (filtered out"
      " ETFs/indices/invalid)."
  )
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


def get_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish reversal candle signal on pullback."""
  total_range = curr_h - curr_l
  if total_range <= 1e-9 or curr_c <= 0:
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

  # 2. Hammer / Strong Lower Wick
  if (
      curr_c >= curr_o
      and (body / curr_c) >= 0.003
      and lower_wick >= 1.8 * body
      and upper_wick <= 0.4 * body
  ):
    return "HAMMER"

  # 3. Bullish Green Candle
  if (
      is_green
      and (body / total_range) >= 0.45
      and ((curr_c - curr_l) / total_range) >= 0.65
  ):
    return "BULLISH_GREEN"

  return ""


def get_first_recent_monthly_swing(df_m, lookback=3):
  """
    Scans from right (most recent completed monthly candle) to left (older candles)
    and returns the VERY FIRST monthly swing high found using `lookback` bars on both sides.
    """
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)

  # Exclude the currently forming last bar (n - 1)
  start_idx = n - 2 - lookback
  if start_idx < lookback:
    return None

  for j in range(start_idx, lookback - 1, -1):
    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if left_ok and right_ok:
      break_dt = None
      for k in range(j + 1, n):
        if float(closes[k]) > val:
          break_dt = dates[k]
          break
      return {
          "swing_date": dates[j],
          "level": val,
          "crossed_monthly": break_dt is not None,
          "break_month_start": break_dt,
      }

  return None


def scan_tomorrow_setups(
    symbol,
    lookback=3,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    zone_tol_pct=1.5,
):
  """
    Returns (crossed_row, pullback_row) for tomorrow's watchlist.
    - crossed_row  : First recent monthly swing (lookback=3) has been crossed/broken.
    - pullback_row : Crossed + >= min_run_pct rally + pullback near Swing_DZ (+- zone_tol_pct).
    """
  crossed_row = None
  pullback_row = None

  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_m = yf.download(
        ticker,
        start="2018-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    df_d = yf.download(
        ticker,
        start="2018-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return None, None
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 205 or len(df_m) < (lookback * 2 + 2):
      return None, None

    sw = get_first_recent_monthly_swing(df_m, lookback=lookback)
    if sw is None:
      return None, None

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    sw_lvl = sw["level"]
    dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
    dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)
    run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)

    last_d = df_d.iloc[-1]
    prev_d = df_d.iloc[-2]
    d_close = float(last_d["Close"])
    d_high = float(last_d["High"])
    d_low = float(last_d["Low"])
    d_open = float(last_d["Open"])
    d_ema200 = float(last_d["EMA200"])
    d_avg_to = float(last_d["Avg_Turnover_20D"])

    if np.isnan(d_avg_to) or d_avg_to < min_turnover:
      return None, None
    if d_close <= d_ema200:
      return None, None

    # Check if swing high is crossed either on monthly close or on daily close/high since swing
    post_swing_d = df_d[df_d.index > sw["swing_date"]]
    if post_swing_d.empty:
      return None, None

    max_post_high = float(post_swing_d["High"].max())
    max_post_close = float(post_swing_d["Close"].max())
    is_crossed = sw["crossed_monthly"] or (max_post_close > sw_lvl)

    if not is_crossed:
      return None, None

    dist_from_swing_pct = ((d_close - sw_lvl) / sw_lvl) * 100.0
    max_run_up_pct = ((max_post_high - sw_lvl) / sw_lvl) * 100.0
    above_200ema_str = "YES" if d_close > d_ema200 else "NO"

    crossed_row = [
        symbol,
        sw["swing_date"].strftime("%Y-%m"),
        round(sw_lvl, 2),
        round(d_close, 2),
        round(dist_from_swing_pct, 2),
        round(max_run_up_pct, 2),
        round(d_avg_to / 10_000_000, 2),  # Turnover in Cr
        above_200ema_str,
        df_d.index[-1].strftime("%Y-%m-%d"),
    ]

    # Pullback check: >= min_run_pct rally happened, price pulled back below max_post_high,
    # and is near the flipped Swing_DZ (+- zone_tol_pct)
    in_or_near_dz = (d_low <= dz_high) and (d_close >= dz_low)
    has_pullback = (max_run_up_pct >= min_run_pct) and (d_close < max_post_high)

    if has_pullback and in_or_near_dz:
      bull_sig = get_bullish_signal(
          float(prev_d["Open"]),
          float(prev_d["High"]),
          float(prev_d["Low"]),
          float(prev_d["Close"]),
          d_open,
          d_high,
          d_low,
          d_close,
      )
      cand_sl = min(d_low, sw_lvl) * 0.99
      risk_pts = d_close - cand_sl
      tp_val = (
          round(d_close + (rr_ratio * risk_pts), 2) if risk_pts > 0 else ""
      )

      pullback_row = [
          symbol,
          sw["swing_date"].strftime("%Y-%m"),
          f"{round(dz_low, 2)}-{round(dz_high, 2)}",
          round(d_close, 2),
          round(max_run_up_pct, 2),
          bull_sig if bull_sig else "WATCH_PULLBACK",
          round(cand_sl, 2),
          tp_val,
          round(d_avg_to / 10_000_000, 2),
          df_d.index[-1].strftime("%Y-%m-%d"),
      ]

    return crossed_row, pullback_row

  except Exception:
    return None, None


def write_tab(sh, tab_name, title_line, headers, rows):
  ws = (
      sh.worksheet(tab_name)
      if tab_name in [x.title for x in sh.worksheets()]
      else sh.add_worksheet(tab_name, 1000, len(headers) + 2)
  )
  ws.clear()
  ws.append_row([title_line])
  ws.append_row(headers)
  if rows:
    ws.append_rows(rows, value_input_option="USER_ENTERED")
  else:
    ws.append_row(["No matching stocks found"])


# -------------------------------------------------------------
# 2. RUN SCAN & UPDATE BOTH TABS FOR TOMORROW
# -------------------------------------------------------------
crossed_list = []
pullback_list = []

for sym in STOCKS:
  c_row, p_row = scan_tomorrow_setups(
      sym,
      lookback=3,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      zone_tol_pct=1.5,
  )
  if c_row:
    crossed_list.append(c_row)
  if p_row:
    pullback_list.append(p_row)

run_stamp = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")

write_tab(
    sh,
    "SWING_CROSSED_READY",
    f"SWING HIGH CROSSED (LOOKBACK=3, LIQUID) | {run_stamp}",
    [
        "Symbol",
        "Swing Date",
        "Swing High",
        "LTP / Close",
        "Diff vs Swing %",
        "Max Run-Up %",
        "Avg Turnover (Cr)",
        "Above 200 EMA",
        "As Of Date",
    ],
    crossed_list,
)

write_tab(
    sh,
    "PULLBACK_NEAR_SWING",
    f"PULLBACK NEAR SWING ZONE (LOOKBACK=3, LIQUID) | {run_stamp}",
    [
        "Symbol",
        "Swing Date",
        "Swing Zone (±1.5%)",
        "LTP / Close",
        "Max Run-Up %",
        "Candle Signal",
        "Indicative SL",
        "Indicative TP (1:2)",
        "Avg Turnover (Cr)",
        "As Of Date",
    ],
    pullback_list,
)

print(
    f"✅ Updated 'SWING_CROSSED_READY' ({len(crossed_list)} stocks) & "
    f"'PULLBACK_NEAR_SWING' ({len(pullback_list)} stocks)!"
)
