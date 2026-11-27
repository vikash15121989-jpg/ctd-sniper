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
    "=== CTD_SNIPER 3-PAGE LIVE & EOD SCANNER (FIRST TRIGGER ONLY ALIGNED) ==="
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


def make_chart_hyperlink(symbol):
  url = f"https://in.tradingview.com/chart/?symbol=NSE:{symbol}"
  return f'=HYPERLINK("{url}", "Chart")'


def get_daily_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish signal name if daily candle at/near flipped swing high is bullish."""
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


def get_monthly_swings_state(df_m, lookback=3, max_age_days=730):
  """
    Finds recent monthly swing highs and tracks their FIRST breakout:
    - FRESH_CROSS: first breakout in current month (`break_idx == n - 1`)
    - BROKEN_SWINGS: all broken monthly swings within max_age_days
    """
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return None, []

  last_dt = dates[-1]
  fresh_cross = None
  broken_swings = []

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

    break_idx = None
    for k in range(j + 1, n):
      if float(closes[k]) > val:
        break_idx = k
        break

    if break_idx is not None:
      info = {
          "swing_date": dates[j],
          "level": val,
          "break_idx": break_idx,
          "break_month_start": dates[break_idx],
      }
      broken_swings.append(info)
      if break_idx == (n - 1):
        fresh_cross = info

  return fresh_cross, broken_swings


def has_earlier_trigger_on_swing(
    sub, sw_lvl, min_run_pct=3.0, zone_tol_pct=1.5, min_turnover=20_000_000
):
  """
    Checks if a valid pullback + bullish signal ALREADY occurred BEFORE the last bar (`len(sub) - 1`).
    If True, today's trigger on this same swing would be a 2nd/repeat attempt.
    """
  if len(sub) < 5:
    return False

  opens = sub["Open"].values
  highs = sub["High"].values
  lows = sub["Low"].values
  closes = sub["Close"].values
  ema200 = sub["EMA200"].values
  avg_to = sub["Avg_Turnover_20D"].values

  run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
  dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
  dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)
  ran_up = False

  # Scan up to second-to-last bar (`len(sub) - 1`)
  for k in range(1, len(sub) - 1):
    if float(highs[k]) >= run_target_lvl:
      ran_up = True
    if not ran_up:
      continue
    if np.isnan(avg_to[k]) or float(avg_to[k]) < min_turnover:
      continue

    d_open = float(opens[k])
    d_high = float(highs[k])
    d_low = float(lows[k])
    d_close = float(closes[k])
    d_ema200 = float(ema200[k])

    above_200_ema = d_close > d_ema200
    touched_or_in_dz = (d_low <= dz_high) and (d_high >= dz_low)

    if above_200_ema and touched_or_in_dz:
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
      if bull_sig and (d_close - min(d_low, sw_lvl) * 0.99) > 0:
        return True

  return False


def scan_stock_three_pages(
    symbol,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    zone_tol_pct=1.5,
    max_dist_above_dz_pct=5.0,
):
  page1_row = None
  page2_row = None
  page3_row = None

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
      return None, None, None
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 205:
      return None, None, None

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    prev_o = float(df_d["Open"].iloc[-2])
    prev_h = float(df_d["High"].iloc[-2])
    prev_l = float(df_d["Low"].iloc[-2])
    prev_c = float(df_d["Close"].iloc[-2])

    curr_o = float(df_d["Open"].iloc[-1])
    day_high = float(df_d["High"].iloc[-1])
    day_low = float(df_d["Low"].iloc[-1])
    cmp = float(df_d["Close"].iloc[-1])
    ema200 = float(df_d["EMA200"].iloc[-1])
    avg_to = float(df_d["Avg_Turnover_20D"].iloc[-1])
    last_date_str = df_d.index[-1].strftime("%Y-%m-%d")

    if np.isnan(avg_to) or avg_to < min_turnover or cmp <= ema200:
      return None, None, None

    avg_to_cr = round(avg_to / 10_000_000.0, 2)
    chart_formula = make_chart_hyperlink(symbol)

    fresh_cross, broken_swings = get_monthly_swings_state(
        df_m, lookback=3, max_age_days=730
    )

    # ---------------------------------------------------------
    # PAGE 1: ABHI-ABHI MONTHLY SWING HIGH CROSS KAR RAHA HAI
    # ---------------------------------------------------------
    if fresh_cross is not None:
      sw_lvl = fresh_cross["level"]
      above_swing_pct = round(((cmp - sw_lvl) / sw_lvl) * 100.0, 2)
      if 0.0 <= above_swing_pct <= 12.0:
        sub_p1 = df_d[df_d.index >= fresh_cross["break_month_start"]]
        max_run_p1 = (
            round(((float(sub_p1["High"].max()) - sw_lvl) / sw_lvl) * 100.0, 2)
            if len(sub_p1) > 0
            else above_swing_pct
        )
        run_status = (
            f"Ran +{max_run_p1}%"
            if max_run_p1 >= min_run_pct
            else f"Early Break (+{max_run_p1}%)"
        )

        page1_row = {
            "Symbol": symbol,
            "Date": last_date_str,
            "Turnover_Cr": avg_to_cr,
            "Swing_Date": fresh_cross["swing_date"].strftime("%Y-%m"),
            "Swing_DZ": round(sw_lvl, 2),
            "Break_Month": fresh_cross["break_month_start"].strftime("%Y-%m"),
            "CMP": round(cmp, 2),
            "Above_Swing_%": above_swing_pct,
            "Breakout_State": run_status,
            "Chart": chart_formula,
        }

    # ---------------------------------------------------------
    # PAGE 2 & PAGE 3: FIRST TRIGGER ONLY ON BROKEN SWING DZ
    # ---------------------------------------------------------
    for sw in reversed(broken_swings):
      sw_lvl = sw["level"]
      sub = df_d[df_d.index >= sw["break_month_start"]]
      if len(sub) < 3:
        continue

      # Skip if a valid pullback trigger already happened earlier on this swing
      if has_earlier_trigger_on_swing(
          sub, sw_lvl, min_run_pct, zone_tol_pct, min_turnover
      ):
        continue

      run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
      dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
      dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)

      max_high_since_break = float(sub["High"].max())
      ran_up_enough = max_high_since_break >= run_target_lvl
      max_run_pct = round(((max_high_since_break - sw_lvl) / sw_lvl) * 100.0, 2)

      if ran_up_enough and cmp >= (sw_lvl * 0.985):
        touched_or_in_dz = (day_low <= dz_high) and (day_high >= dz_low)
        if touched_or_in_dz:
          dist_pct = 0.0
          zone_state = "AT / INSIDE FLIPPED DZ"
        elif cmp > dz_high:
          dist_pct = round(((cmp - dz_high) / cmp) * 100.0, 2)
          zone_state = f"{dist_pct}% Above Flipped DZ"
        else:
          dist_pct = 0.0
          zone_state = "Near Flipped DZ"

        sl_price = round(min(day_low, sw_lvl) * 0.99, 2)
        risk_pts = cmp - sl_price
        risk_pct = round((risk_pts / cmp) * 100.0, 2) if risk_pts > 0 else 0.0
        tp_price = round(cmp + (2.0 * risk_pts), 2) if risk_pts > 0 else 0.0

        if page2_row is None and dist_pct <= max_dist_above_dz_pct:
          page2_row = {
              "Symbol": symbol,
              "Date": last_date_str,
              "Turnover_Cr": avg_to_cr,
              "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
              "Swing_DZ": round(sw_lvl, 2),
              "Run_Up_%": max_run_pct,
              "CMP": round(cmp, 2),
              "Dist_From_DZ_%": dist_pct,
              "Zone_Status": zone_state,
              "SL_Price": sl_price,
              "Target_1to2": tp_price,
              "Chart": chart_formula,
          }

        near_flipped_dz = touched_or_in_dz or (dist_pct <= 2.0)
        if page3_row is None and near_flipped_dz and risk_pts > 0:
          bull_sig = get_daily_bullish_signal(
              prev_o, prev_h, prev_l, prev_c, curr_o, day_high, day_low, cmp
          )
          if bull_sig:
            if bull_sig in ["BULLISH_ENGULFING", "HAMMER"] and risk_pct <= 4.5:
              priority_tag = "1_TOP_PICK"
            elif risk_pct <= 4.5:
              priority_tag = "2_GOOD_PULLBACK"
            else:
              priority_tag = "3_HIGH_RISK_SL"

            page3_row = {
                "Priority": priority_tag,
                "Symbol": symbol,
                "EOD_Date": last_date_str,
                "Turnover_Cr": avg_to_cr,
                "Bullish_Signal": bull_sig,
                "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
                "Swing_DZ": round(sw_lvl, 2),
                "Run_Up_%": max_run_pct,
                "Dist_From_DZ_%": dist_pct,
                "EOD_Close": round(cmp, 2),
                "SL_Price": sl_price,
                "Risk_%": risk_pct,
                "Target_1to2": tp_price,
                "Chart": chart_formula,
            }

      if page2_row is not None and page3_row is not None:
        break

    return page1_row, page2_row, page3_row
  except Exception:
    return None, None, None


# -------------------------------------------------------------
# 2. RUN SCANNER & WRITE ALL 3 PAGES TO GOOGLE SHEET
# -------------------------------------------------------------
p1_list, p2_list, p3_list = [], [], []

for sym in STOCKS:
  r1, r2, r3 = scan_stock_three_pages(
      sym,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      zone_tol_pct=1.5,
      max_dist_above_dz_pct=5.0,
  )
  if r1 is not None:
    p1_list.append(r1)
  if r2 is not None:
    p2_list.append(r2)
  if r3 is not None:
    p3_list.append(r3)

now_str = datetime.now(IST).strftime("%d-%b-%Y %I:%M %p IST")

# Write Tab 1: 1_FRESH_SWING_CROSS
try:
  t1 = "1_FRESH_SWING_CROSS"
  ws1 = (
      sh.worksheet(t1)
      if t1 in [x.title for x in sh.worksheets()]
      else sh.add_worksheet(t1, 500, 12)
  )
  ws1.clear()
  ws1.append_row([f"PAGE 1: FRESH MONTHLY SWING CROSS | Run: {now_str}"])
  ws1.append_row([
      "Symbol",
      "Date",
      "Turnover (Cr)",
      "Swing Date",
      "Swing DZ",
      "Break Month",
      "CMP",
      "Above Swing %",
      "Breakout State",
      "Chart",
  ])
  if p1_list:
    df1 = pd.DataFrame(p1_list).sort_values(by="Above_Swing_%", ascending=True)
    ws1.append_rows(df1.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Page 1 ('{t1}'): {len(df1)} stocks saved.")
  else:
    ws1.append_row(["No fresh monthly swing cross found today."])
    print(f"⚠️ Page 1 ('{t1}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 1 Sheet Error: {e}")

# Write Tab 2: 2_BREAKOUT_CORRECTION_DZ
try:
  t2 = "2_BREAKOUT_CORRECTION_DZ"
  ws2 = (
      sh.worksheet(t2)
      if t2 in [x.title for x in sh.worksheets()]
      else sh.add_worksheet(t2, 500, 13)
  )
  ws2.clear()
  ws2.append_row([
      "PAGE 2: RUN-UP + CORRECTION TO FLIPPED SWING DZ (1ST ATTEMPT) |"
      f" Run: {now_str}"
  ])
  ws2.append_row([
      "Symbol",
      "Date",
      "Turnover (Cr)",
      "Swing Date",
      "Swing DZ",
      "Run Up %",
      "CMP",
      "Dist From DZ %",
      "Zone Status",
      "SL Price",
      "Target (1:2)",
      "Chart",
  ])
  if p2_list:
    df2 = pd.DataFrame(p2_list).sort_values(
        by="Dist_From_DZ_%", ascending=True
    )
    ws2.append_rows(df2.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Page 2 ('{t2}'): {len(df2)} stocks saved.")
  else:
    ws2.append_row(["No correction near flipped swing DZ found today."])
    print(f"⚠️ Page 2 ('{t2}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 2 Sheet Error: {e}")

# Write Tab 3: 3_TOMORROW_READY_EOD
try:
  t3 = "3_TOMORROW_READY_EOD"
  ws3 = (
      sh.worksheet(t3)
      if t3 in [x.title for x in sh.worksheets()]
      else sh.add_worksheet(t3, 500, 15)
  )
  ws3.clear()
  ws3.append_row([
      "PAGE 3: TOMORROW READY EOD (1ST ATTEMPT AT/NEAR FLIPPED DZ + BULLISH"
      f" SIGNAL) | Run: {now_str}"
  ])
  ws3.append_row([
      "Priority",
      "Symbol",
      "EOD Date",
      "Turnover (Cr)",
      "Bullish Signal",
      "Swing Date",
      "Swing DZ",
      "Run Up %",
      "Dist From DZ %",
      "EOD Close",
      "SL Price",
      "Risk %",
      "Target (1:2)",
      "Chart",
  ])
  if p3_list:
    df3 = pd.DataFrame(p3_list).sort_values(
        by=["Priority", "Dist_From_DZ_%"], ascending=[True, True]
    )
    ws3.append_rows(df3.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Page 3 ('{t3}'): {len(df3)} stocks ready for tomorrow!")
  else:
    ws3.append_row(["No EOD bullish signal near flipped swing DZ today."])
    print(f"⚠️ Page 3 ('{t3}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 3 Sheet Error: {e}")
