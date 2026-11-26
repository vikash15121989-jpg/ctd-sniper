from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER 3-PAGE LIVE & EOD SCANNER (WITH TOMORROW READY TAB) ===")

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


def detect_eod_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c, curr_v, vol_avg
):
  """
    Checks EOD bullish confirmation near Demand Zone:
    1. Bullish Engulfing
    2. Bullish Hammer / Pin-bar rejection from low
    3. Strong Green Close (close in top 30% of candle range + green body)
    4. High Volume Bullish Candle (green candle with >= 1.4x 20-day avg volume)
    """
  signals = []
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
    signals.append("Bullish Engulfing")

  # 2. Hammer / Strong Lower Wick Rejection
  if (
      curr_c >= curr_o
      and body > 1e-9
      and lower_wick >= 1.8 * body
      and upper_wick <= 0.4 * body
  ):
    signals.append("Hammer / Rejection")

  # 3. Strong Green Close (closes near day high after touching/nearing DZ)
  close_pos = (curr_c - curr_l) / total_range
  if is_green and close_pos >= 0.72 and (body / total_range) >= 0.45:
    signals.append("Strong Green Close")

  # 4. Volume-backed Bullish Candle
  if (
      is_green
      and not np.isnan(vol_avg)
      and vol_avg > 0
      and curr_v >= 1.4 * vol_avg
  ):
    signals.append(f"Vol Spike ({round(curr_v / vol_avg, 1)}x)")

  return " + ".join(signals)


def get_monthly_swings_state(df_m, lookback=4, max_age_days=365):
  """
    Finds strong monthly swing highs within `max_age_days` (default 1 year):
    - FRESH_CROSS: first breakout in current month (`break_idx == n - 1`)
    - BROKEN_ACTIVE: earlier breakout within window
    """
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return None, None

  last_dt = dates[-1]
  fresh_cross = None
  older_broken = None

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
      if break_idx == (n - 1):
        fresh_cross = info
      else:
        older_broken = info

  return fresh_cross, older_broken


def find_post_break_demand_zone(df_d, start_dt, level):
  """Finds a clean post-breakout Daily Demand Zone."""
  sub = df_d[df_d.index >= start_dt]
  if len(sub) < 4:
    return None

  opens = sub["Open"].values
  highs = sub["High"].values
  lows = sub["Low"].values
  closes = sub["Close"].values

  for i in range(1, len(sub) - 2):
    if float(closes[i]) <= level:
      continue

    body_base = abs(float(closes[i]) - float(opens[i]))
    range_base = max(float(highs[i]) - float(lows[i]), 1e-9)
    next_open = float(opens[i + 1])
    next_close = float(closes[i + 1])
    next_body = next_close - next_open

    if (
        body_base <= 0.45 * range_base
        and next_close > next_open
        and next_body > 1.8 * max(body_base, 1e-9)
    ):
      dz_low = float(lows[i])
      dz_high = max(float(opens[i]), float(closes[i]), float(highs[i]))
      return dz_low, dz_high, sub.index[i + 1]

  return None


def scan_stock_three_pages(
    symbol, min_turnover=50_000_000, max_dist_above_dz_pct=5.0
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
    df_d["Vol_SMA20"] = df_d["Volume"].rolling(window=20).mean()

    prev_o = float(df_d["Open"].iloc[-2])
    prev_h = float(df_d["High"].iloc[-2])
    prev_l = float(df_d["Low"].iloc[-2])
    prev_c = float(df_d["Close"].iloc[-2])

    curr_o = float(df_d["Open"].iloc[-1])
    day_high = float(df_d["High"].iloc[-1])
    day_low = float(df_d["Low"].iloc[-1])
    cmp = float(df_d["Close"].iloc[-1])
    curr_v = float(df_d["Volume"].iloc[-1])

    ema200 = float(df_d["EMA200"].iloc[-1])
    avg_to = float(df_d["Avg_Turnover_20D"].iloc[-1])
    vol_avg = float(df_d["Vol_SMA20"].iloc[-1])
    last_date_str = df_d.index[-1].strftime("%Y-%m-%d")

    if np.isnan(avg_to) or avg_to < min_turnover or cmp <= ema200:
      return None, None, None

    avg_to_cr = round(avg_to / 10_000_000.0, 2)
    chart_formula = make_chart_hyperlink(symbol)

    fresh_cross, older_broken = get_monthly_swings_state(
        df_m, lookback=4, max_age_days=365
    )

    # ---------------------------------------------------------
    # PAGE 1: CURRENT MONTH FRESH SWING CROSS (<= 10% ABOVE SWING)
    # ---------------------------------------------------------
    if fresh_cross is not None:
      sw_lvl = fresh_cross["level"]
      above_swing_pct = round(((cmp - sw_lvl) / sw_lvl) * 100.0, 2)
      if 0.0 <= above_swing_pct <= 10.0:
        dz = find_post_break_demand_zone(
            df_d, fresh_cross["break_month_start"], sw_lvl
        )
        dz_status = (
            f"DZ Ready ({round(dz[0],2)}-{round(dz[1],2)})"
            if dz is not None
            else "Waiting for Daily DZ"
        )

        page1_row = {
            "Symbol": symbol,
            "Date": last_date_str,
            "Turnover_Cr": avg_to_cr,
            "Swing_Date": fresh_cross["swing_date"].strftime("%Y-%m"),
            "Swing_High": round(sw_lvl, 2),
            "Break_Month": fresh_cross["break_month_start"].strftime("%Y-%m"),
            "CMP": round(cmp, 2),
            "Above_Swing_%": above_swing_pct,
            "DZ_Status": dz_status,
            "Chart": chart_formula,
        }

    # ---------------------------------------------------------
    # PAGE 2 & PAGE 3: CORRECTION NEAR DZ & TOMORROW READY EOD
    # ---------------------------------------------------------
    target_sw = older_broken if older_broken is not None else fresh_cross
    if target_sw is not None:
      sw_lvl = target_sw["level"]
      dz = find_post_break_demand_zone(
          df_d, target_sw["break_month_start"], sw_lvl
      )
      if dz is not None:
        dz_low, dz_high, dz_dt = dz
        sub_after_dz = df_d[df_d.index > dz_dt]
        if len(sub_after_dz) >= 1:
          peak_after_dz = float(sub_after_dz["High"].max())
          pullback_from_peak_pct = ((peak_after_dz - cmp) / peak_after_dz) * 100.0
          is_correcting = (pullback_from_peak_pct >= 4.0) and (
              cmp >= dz_low * 0.99
          )

          if is_correcting:
            touched_today = (day_low <= dz_high) and (day_high >= dz_low)
            if touched_today:
              dist_pct = 0.0
              zone_state = "INSIDE ZONE"
            elif cmp > dz_high:
              dist_pct = round(((cmp - dz_high) / cmp) * 100.0, 2)
              zone_state = f"{dist_pct}% Above DZ"
            else:
              dist_pct = 0.0
              zone_state = "At DZ Bottom"

            sl_price = round(dz_low * 0.99, 2)
            risk_pts = cmp - sl_price
            risk_pct = round((risk_pts / cmp) * 100.0, 2) if risk_pts > 0 else 0.0
            tp_price = round(cmp + (2.0 * risk_pts), 2) if risk_pts > 0 else 0.0

            # PAGE 2: Within 5% of Demand Zone
            if dist_pct <= max_dist_above_dz_pct:
              page2_row = {
                  "Symbol": symbol,
                  "Date": last_date_str,
                  "Turnover_Cr": avg_to_cr,
                  "Swing_High": round(sw_lvl, 2),
                  "Break_Month": target_sw["break_month_start"].strftime(
                      "%Y-%m"
                  ),
                  "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
                  "CMP": round(cmp, 2),
                  "Dist_From_DZ_%": dist_pct,
                  "Zone_Status": zone_state,
                  "SL_Price": sl_price,
                  "TP_1to2": tp_price,
                  "Chart": chart_formula,
              }

            # PAGE 3: TOMORROW READY EOD (Inside DZ or <= 2% above DZ + Bullish Signal)
            near_or_inside_dz = touched_today or (dist_pct <= 2.0)
            if near_or_inside_dz and risk_pts > 0:
              bull_sig = detect_eod_bullish_signal(
                  prev_o,
                  prev_h,
                  prev_l,
                  prev_c,
                  curr_o,
                  day_high,
                  day_low,
                  cmp,
                  curr_v,
                  vol_avg,
              )
              if bull_sig:
                page3_row = {
                    "Symbol": symbol,
                    "EOD_Date": last_date_str,
                    "Turnover_Cr": avg_to_cr,
                    "Bullish_Signal": bull_sig,
                    "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
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
  r1, r2, r3 = scan_stock_three_pages(
      sym, min_turnover=50_000_000, max_dist_above_dz_pct=5.0
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
      "Swing High",
      "Break Month",
      "CMP",
      "Above Swing %",
      "DZ Status",
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
      else sh.add_worksheet(t2, 500, 14)
  )
  ws2.clear()
  ws2.append_row(
      [f"PAGE 2: NEAR / INSIDE DEMAND ZONE (<= 5% ABOVE DZ) | Run: {now_str}"]
  )
  ws2.append_row([
      "Symbol",
      "Date",
      "Turnover (Cr)",
      "Swing High",
      "Break Month",
      "DZ Range",
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
    ws2.append_row(["No correction stock close to or inside DZ found today."])
    print(f"⚠️ Page 2 ('{t2}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 2 Sheet Error: {e}")

# Write Tab 3: 3_TOMORROW_READY_EOD
try:
  t3 = "3_TOMORROW_READY_EOD"
  ws3 = (
      sh.worksheet(t3)
      if t3 in [x.title for x in sh.worksheets()]
      else sh.add_worksheet(t3, 500, 13)
  )
  ws3.clear()
  ws3.append_row([
      "PAGE 3: TOMORROW READY EOD SIGNALS (NEAR/INSIDE DZ + BULLISH CANDLE) |"
      f" Run: {now_str}"
  ])
  ws3.append_row([
      "Symbol",
      "EOD Date",
      "Turnover (Cr)",
      "Bullish Signal",
      "DZ Range",
      "Dist From DZ %",
      "EOD Close",
      "SL Price",
      "Risk %",
      "Target (1:2)",
      "Chart",
  ])
  if p3_list:
    df3 = pd.DataFrame(p3_list).sort_values(
        by="Dist_From_DZ_%", ascending=True
    )
    ws3.append_rows(df3.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Page 3 ('{t3}'): {len(df3)} stocks ready for tomorrow!")
  else:
    ws3.append_row(["No EOD bullish signal near Demand Zone today."])
    print(f"⚠️ Page 3 ('{t3}'): 0 stocks.")
except Exception as e:
  print(f"❌ Page 3 Sheet Error: {e}")
