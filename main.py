import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: FULL TRADINGVIEW ENGINE WITH POSITION SIZING ===", flush=True)

# ===== CONFIGURATION & RISK SETTINGS =====
TOTAL_CAPITAL = 100000.0           # Aapka total account balance (₹1 Lakh)
RISK_PER_TRADE_PCT = 1.0          # Risk 1% per trade (₹1,000 max loss per trade)
MAX_RISK_AMOUNT = TOTAL_CAPITAL * (RISK_PER_TRADE_PCT / 100.0)

MIN_VOLUME_UNITS = 100_000         # 1 Lakh Minimum Shares (Small/Mid-caps filter)
MIN_TURNOVER_VALUE = 5_000_000     # ₹50 Lakh Minimum Turnover
ZIGZAG_DEPTH = 10                  # TradingView Default Depth
VOL_SURGE_MULTIPLIER = 2.5         # Volume >= 2.5x of 20-Day Avg
MIN_ROOM_TO_RES_PCT = 5.0          # Minimum 5% Overhead Gap

END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=365)

# ===== GOOGLE SHEETS SETUP =====
gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

def get_or_create_sheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="15")

ws_watchlist = sh.worksheet("Watchlist")
ws_zigzag_filtered = get_or_create_sheet("ZigZag_10pct_Filtered")
ws_vol_breakout = get_or_create_sheet("Volume_Breakout_Watchlist")
ws_active_breakouts = get_or_create_sheet("Active_Breakouts")
ws_position_sizing = get_or_create_sheet("Position_Sizing")

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]]
    return [s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s for s in stocks]


# ===== TRADINGVIEW EXACT ZIGZAG ALGORITHM =====
def get_tradingview_exact_zigzag(df, depth=ZIGZAG_DEPTH):
    highs = df["High"].values
    lows = df["Low"].values
    dates = df.index
    n = len(df)

    if n < (depth * 2 + 1):
        return [], []

    pivot_highs = []
    pivot_lows = []

    # Pivot High / Pivot Low Matching (ta.pivothigh / ta.pivotlow)
    for i in range(depth, n - depth):
        current_high = highs[i]
        current_low = lows[i]

        is_high = all(current_high > highs[i - j] for j in range(1, depth + 1)) and \
                  all(current_high >= highs[i + j] for j in range(1, depth + 1))

        is_low = all(current_low < lows[i - j] for j in range(1, depth + 1)) and \
                 all(current_low <= lows[i + j] for j in range(1, depth + 1))

        if is_high:
            pivot_highs.append((i, current_high, dates[i]))
        if is_low:
            pivot_lows.append((i, current_low, dates[i]))

    all_pivots = sorted(
        [(idx, price, dt, 'H') for idx, price, dt in pivot_highs] + 
        [(idx, price, dt, 'L') for idx, price, dt in pivot_lows],
        key=lambda x: x[0]
    )

    if not all_pivots:
        return [], []

    clean_swings = [all_pivots[0]]
    for curr in all_pivots[1:]:
        last = clean_swings[-1]
        
        if curr[3] == 'H' and last[3] == 'H':
            if curr[1] > last[1]:
                clean_swings[-1] = curr
        elif curr[3] == 'L' and last[3] == 'L':
            if curr[1] < last[1]:
                clean_swings[-1] = curr
        else:
            clean_swings.append(curr)

    final_highs = [p for p in clean_swings if p[3] == 'H']
    final_lows = [p for p in clean_swings if p[3] == 'L']

    return final_highs, final_lows


# ===== OVERHEAD RESISTANCE EVALUATOR =====
def evaluate_overhead_resistance(swing_highs):
    if not swing_highs:
        return False, None, None, 0.0

    h1_price = swing_highs[-1][1]
    higher_swings = [sh[1] for sh in swing_highs[:-1] if sh[1] > h1_price]

    if not higher_swings:
        return True, h1_price, None, 999.0

    hprev_price = higher_swings[-1]
    gap_pct = ((hprev_price - h1_price) / h1_price) * 100.0

    if gap_pct >= MIN_ROOM_TO_RES_PCT:
        return True, h1_price, hprev_price, gap_pct

    return False, h1_price, hprev_price, gap_pct


# ===== MAIN EXECUTION PIPELINE =====
stocks = get_watchlist_stocks()

sheet1_data = []
sheet2_data = []
sheet3_data = []
position_sizing_data = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        latest_vol = df.iloc[-1]["Volume"]
        latest_turnover = latest_vol * df.iloc[-1]["Close"]
        if not ((latest_vol >= MIN_VOLUME_UNITS) or (latest_turnover >= MIN_TURNOVER_VALUE)):
            continue

        df["Vol_Avg20"] = df["Volume"].rolling(window=20).mean()
        view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'

        # Current Chart Structure
        all_highs, all_lows = get_tradingview_exact_zigzag(df, depth=ZIGZAG_DEPTH)
        is_valid_res, entry_h1, res_hprev, gap_pct = evaluate_overhead_resistance(all_highs)

        if is_valid_res and all_lows:
            sheet1_data.append({
                "Stock": symbol_clean,
                "Current_Close": round(df.iloc[-1]["Close"], 2),
                "Recent_Swing_High_H1": round(entry_h1, 2),
                "Overhead_Resistance_Hprev": round(res_hprev, 2) if res_hprev else "Open Sky",
                "Gap_%": round(gap_pct, 2) if res_hprev else "Open Sky",
                "Stop_Loss": round(all_lows[-1][1], 2),
                "View_Chart": view_chart_link
            })

        # Scan Last 10 Days For 1-Day Before Volume Spike
        last_10_df = df.iloc[-10:]

        for idx, row in last_10_df.iterrows():
            spike_idx = df.index.get_loc(idx)
            if spike_idx < 30:
                continue

            prev_vol_avg = df.iloc[spike_idx - 1]["Vol_Avg20"]
            current_vol = row["Volume"]

            if prev_vol_avg > 0 and current_vol >= (VOL_SURGE_MULTIPLIER * prev_vol_avg):
                df_until_spike = df.iloc[: spike_idx + 1]
                sp_highs, sp_lows = get_tradingview_exact_zigzag(df_until_spike, depth=ZIGZAG_DEPTH)

                if not sp_highs or not sp_lows:
                    continue

                sp_valid, sp_h1, sp_hprev, sp_gap_pct = evaluate_overhead_resistance(sp_highs)

                if sp_valid and row["High"] < sp_h1:
                    spike_date = idx.strftime("%Y-%m-%d")

                    sheet2_data.append({
                        "Spike_Date": spike_date,
                        "Stock": symbol_clean,
                        "Recent_Swing_High_H1": round(sp_h1, 2),
                        "Overhead_Resistance_Hprev": round(sp_hprev, 2) if sp_hprev else "Open Sky",
                        "Gap_%": round(sp_gap_pct, 2) if sp_hprev else "Open Sky",
                        "Stop_Loss": round(sp_lows[-1][1], 2),
                        "View_Chart": view_chart_link
                    })

                    post_spike_df = df.iloc[spike_idx:]
                    current_close = df.iloc[-1]["Close"]
                    dist_to_h1_pct = round(((sp_h1 - current_close) / current_close) * 100, 2)
                    stop_loss_price = round(sp_lows[-1][1], 2)

                    if post_spike_df["High"].max() >= sp_h1:
                        status = "🔥 Entry Triggered"
                        dist_str = "0.00% (Triggered)"
                    else:
                        status = "🎯 Entry Pending"
                        dist_str = f"{dist_to_h1_pct}% Away"

                    sheet3_data.append({
                        "Spike_Date": spike_date,
                        "Stock": symbol_clean,
                        "Status": status,
                        "Current_Price": round(current_close, 2),
                        "Recent_Swing_High_H1": round(sp_h1, 2),
                        "Distance_To_Entry": dist_str,
                        "Stop_Loss": stop_loss_price,
                        "View_Chart": view_chart_link
                    })

                    # Position Sizing Logic Calculation
                    risk_per_share = sp_h1 - stop_loss_price
                    if risk_per_share > 0:
                        qty = int(MAX_RISK_AMOUNT / risk_per_share)
                        total_inv = round(qty * sp_h1, 2)

                        position_sizing_data.append({
                            "Spike_Date": spike_date,
                            "Stock": symbol_clean,
                            "Status": status,
                            "Buy_Trigger_Price": round(sp_h1, 2),
                            "Stop_Loss": stop_loss_price,
                            "Risk_Per_Share": round(risk_per_share, 2),
                            "Recommended_Qty": qty,
                            "Total_Investment": f"₹{total_inv}",
                            "Max_Risk": f"₹{int(MAX_RISK_AMOUNT)}",
                            "View_Chart": view_chart_link
                        })
                    break

    except Exception:
        pass


# ===== UPLOAD ALL TABS TO GOOGLE SHEETS =====
ws_zigzag_filtered.clear()
if sheet1_data:
    df_s1 = pd.DataFrame(sheet1_data)
    json_s1 = json.loads(df_s1.to_json(orient="split"))
    ws_zigzag_filtered.update(values=[json_s1["columns"]] + json_s1["data"], range_name="A1", value_input_option="USER_ENTERED")

ws_vol_breakout.clear()
if sheet2_data:
    df_s2 = pd.DataFrame(sheet2_data).sort_values(by="Spike_Date", ascending=False)
    json_s2 = json.loads(df_s2.to_json(orient="split"))
    ws_vol_breakout.update(values=[json_s2["columns"]] + json_s2["data"], range_name="A1", value_input_option="USER_ENTERED")

ws_active_breakouts.clear()
if sheet3_data:
    df_s3 = pd.DataFrame(sheet3_data).sort_values(by="Spike_Date", ascending=False)
    json_s3 = json.loads(df_s3.to_json(orient="split"))
    ws_active_breakouts.update(values=[json_s3["columns"]] + json_s3["data"], range_name="A1", value_input_option="USER_ENTERED")

# Position_Sizing Tab Data Sync
ws_position_sizing.clear()
if position_sizing_data:
    df_ps = pd.DataFrame(position_sizing_data).sort_values(by="Spike_Date", ascending=False)
    json_ps = json.loads(df_ps.to_json(orient="split"))
    ws_position_sizing.update(values=[json_ps["columns"]] + json_ps["data"], range_name="A1", value_input_option="USER_ENTERED")
    print("✅ All tabs updated including Position_Sizing successfully!", flush=True)
else:
    ws_position_sizing.update(values=[["No Active Candidates For Position Sizing"]], range_name="A1")
    
