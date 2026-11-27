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
    "=== CTD_SNIPER SCANNER: RECENT BREAKOUT (<=20 TRADING DAYS) + RUN-UP +"
    " CORRECTION ==="
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
  """Returns strong bullish reversal signal."""
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


def get_recent_broken_swing(
    df_m,
    df_d,
    lookback=3,
    max_swing_age_days=730,
    max_break_trading_days=20,
):
  """
    Finds the most recent broken monthly swing high whose breakout window
    is within `max_break_trading_days` (default 20 daily trading bars).
    """
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2) or df_d.empty:
    return None

  last_monthly_dt = dates[-1]
  recent_break = None

  for j in range(lookback, n - lookback):
    if (last_monthly_dt - dates[j]).days > max_swing_age_days:
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
        break_m_start = dates[k]
        # Count daily trading bars from the start of the breakout month up to latest daily bar
        sub_d = df_d[df_d.index >= break_m_start]
        if len(sub_d) > 0:
          trading_days_since = len(sub_d) - 1
          # If breakout happened in the latest monthly candle (`k == n - 1`),
          # find the first daily bar where price moved/closed above swing level if available
          if k == (n - 1):
            above_bars = sub_d[sub_d["Close"] > val]
            if len(above_bars) > 0:
              first_break_dt = above_bars.index[0]
              trading_days_since = int(
                  (df_d.index >= first_break_dt).sum() - 1
              )

          if 0 <= trading_days_since <= max_break_trading_days:
            recent_break = {
                "swing_date": dates[j],
                "level": val,
                "break_idx": k,
                "break_month_start": break_m_start,
                "break_trading_days": trading_days_since,
            }
        break

  return recent_break


def scan_recent_breakout_pullback(
    symbol,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    zone_tol_pct=1.5,
    max_dist_above_dz_pct=5.0,
    max_break_trading_days=20,
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

    rec_break = get_recent_broken_swing(
        df_m,
        df_d,
        lookback=3,
        max_swing_age_days=730,
        max_break_trading_days=max_break_trading_days,
    )
    if rec_break is None:
      return None, None, None

    sw_lvl = rec_break["level"]
    sub = df_d[df_d.index >= rec_break["break_month_start"]]
    if len(sub) < 2:
      return None, None, None

    avg_to_cr = round(avg_to / 10_000_000.0, 2)
    chart_formula = make_chart_hyperlink(symbol)

    max_high_since_break = float(sub["High"].max())
    max_run_pct = round(((max_high_since_break - sw_lvl) / sw_lvl) * 100.0, 2)
    above_swing_pct = round(((cmp - sw_lvl) / sw_lvl) * 100.0, 2)
    break_td = rec_break["break_trading_days"]

    # PAGE 1: Recent breakouts within max_break_trading_days
    if -2.0 <= above_swing_pct <= 15.0:
      run_status = (
          f"Ran +{max_run_pct}% ({break_td}td ago)"
          if max_run_pct >= min_run_pct
          else f"Early Break +{max_run_pct}% ({break_td}td ago)"
      )
      page1_row = {
          "Symbol": symbol,
          "Date": last_date_str,
          "Turnover_Cr": avg_to_cr,
          "Swing_Date": rec_break["swing_date"].strftime("%Y-%m"),
          "Swing_DZ": round(sw_lvl, 2),
          "Break_Month": rec_break["break_month_start"].strftime("%Y-%m"),
          "CMP": round(cmp, 2),
          "Above_Swing_%": above_swing_pct,
          "Breakout_State": run_status,
          "Chart": chart_formula,
      }

    # Must have run up >= min_run_pct after recent breakout and now be correcting back toward Swing_DZ
    run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
    is_correcting_from_high = (max_high_since_break >= run_target_lvl) and (
        cmp < max_high_since_break
    )

    if is_correcting_from_high and cmp >= (sw_lvl * 0.985):
      dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
      dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)

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

      # PAGE 2: Recent breakout (<=20 trading days) + >=3% run-up + correcting within 5% of Swing_DZ
      if dist_pct <= max_dist_above_dz_pct:
        page2_row = {
            "Symbol": symbol,
            "Date": last_date_str,
            "Turnover_Cr": avg_to_cr,
            "Swing_Date": rec_break["swing_date"].strftime("%Y-%m"),
            "Swing_DZ": round(sw_lvl, 2),
            "Run_Up_%": max_run_pct,
            "CMP": round(cmp, 2),
            "Dist_From_DZ_%": dist_pct,
            "Zone_Status": zone_state,
            "SL_Price": sl_price,
            "Target_1to2": tp_price,
            "Chart": chart_formula,
        }

      # PAGE 3: Recent breakout (<=20 trading days) + >=3% run-up + correcting near DZ + Bullish Signal
      near_flipped_dz = touched_or_in_dz or (dist_pct <= 2.0)
      if near_flipped_dz and risk_pts > 0:
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
              "Swing_Date": rec_break["swing_date"].strftime("%Y-%m"),
              "Swing_DZ": round(sw_lvl, 2),
              "Run_Up_%": max_run_pct,
              "Dist_From_DZ_%": dist_pct,
              "EOD_Close": round(cmp, 2),
              "SL_Price": sl_price,
              "Risk_%": risk_pct,
              "Target_1to2": tp_price,
              "Chart": chart_formula,
          }

    return page1_row, page2_row, page3_row
  except Exception:
    return None, None, None


# -------------------------------------------------------------
# 2. RUN SCANNER & WRITE ALL 3 PAGES TO GOOGLE SHEET
# -------------------------------------------------------------
p1_list, p2_list, p3_list = [], [], []

for sym in STOCKS:
  r1, r2, r3 = scan_recent_breakout_pullback(
      sym,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      zone_tol_pct=1.5,
      max_dist_above_dz_pct=5.0,
      max_break_trading_days=20,
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
  ws1.append_row([
      "PAGE 1: RECENT MONTHLY SWING BREAKOUTS (<=20 TRADING DAYS) |"
      f" Run: {now_str}"
  ])
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
    ws1.append_row(["No recent monthly swing breakout found today."])
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
      "PAGE 2: RECENT BREAKOUT (<=20 TD) + RUN-UP + CORRECTION TO DZ |"
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
    ws2.append_row(["No recent breakout correction near DZ found today."])
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
      "PAGE 3: RECENT BREAKOUT (<=20 TD) + CORRECTION + BULLISH SIGNAL |"
      f" Run: {now_str}"
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
    ws3.append_row(["No recent breakout correction bullish signal today."])
    print(f"⚠️ Page 3 ('{t3}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 3 Sheet Error: {e}")
              
