import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: EXACT RESISTANCE & VOLUME INTACT FILTER ===", flush=True)

# ===== CONFIGURATION =====
MIN_VOLUME_UNITS = 5_000_000       # Minimum 50 Lakh Volume
MIN_TURNOVER_VALUE = 20_000_000    # Minimum ₹2 Crore Trading Value
ZIGZAG_DEV_PCT = 0.05              # 5% Reversal
PIVOT_LEGS = 10                    # 10 Legs
MIN_ROOM_TO_RES_PCT = 10.0         # Minimum 10% Distance to Overhead Resistance
LOOKBACK_DAYS = 365

END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=LOOKBACK_DAYS)

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
ws_pos_sizing = get_or_create_sheet("Position_Sizing")

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]]
    return [s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s for s in stocks]


# ===== TRADINGVIEW ZIG ZAG ALGORITHM =====
def get_zigzag_swings(df, dev_pct=ZIGZAG_DEV_PCT, legs=PIVOT_LEGS):
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)
    
    if n < (legs * 2 + 1):
        return [], []

    pivots = []
    for i in range(legs, n - legs):
        if all(highs[i] >= highs[i - legs : i]) and all(highs[i] >= highs[i + 1 : i + legs + 1]):
            pivots.append((i, highs[i], 'H'))
        if all(lows[i] <= lows[i - legs : i]) and all(lows[i] <= lows[i + 1 : i + legs + 1]):
            pivots.append((i, lows[i], 'L'))

    if not pivots:
        return [], []

    swing_highs = []
    swing_lows = []
    last_type, last_price, last_idx = None, None, None

    for idx, price, p_type in pivots:
        if last_type is None:
            last_type, last_price, last_idx = p_type, price, idx
            if p_type == 'H': swing_highs.append((idx, price))
            else: swing_lows.append((idx, price))
            continue

        if p_type == 'H' and last_type == 'L':
            if price >= last_price * (1 + dev_pct):
                swing_highs.append((idx, price))
                last_type, last_price, last_idx = 'H', price, idx
        elif p_type == 'L' and last_type == 'H':
            if price <= last_price * (1 - dev_pct):
                swing_lows.append((idx, price))
                last_type, last_price, last_idx = 'L', price, idx
        elif p_type == 'H' and last_type == 'H':
            if price > last_price:
                if swing_highs and swing_highs[-1][0] == last_idx: swing_highs.pop()
                swing_highs.append((idx, price))
                last_price, last_idx = price, idx
        elif p_type == 'L' and last_type == 'L':
            if price < last_price:
                if swing_lows and swing_lows[-1][0] == last_idx: swing_lows.pop()
                swing_lows.append((idx, price))
                last_price, last_idx = price, idx

    return swing_highs, swing_lows


# ===== STEP 1: LIQUIDITY CHECK =====
def is_liquid_stock(df):
    latest_bar = df.iloc[-1]
    latest_vol = latest_bar["Volume"]
    latest_turnover = latest_vol * latest_bar["Close"]
    return (latest_vol >= MIN_VOLUME_UNITS) or (latest_turnover >= MIN_TURNOVER_VALUE)


# ===== OVERHEAD RESISTANCE FINDER (AAPKA EXACT LOGIC) =====
def check_resistance_distance(swing_highs):
    """
    Recent Swing High (Entry) se peeche BADA Swing High (Resistance) dhundta hai.
    Agar Recent Swing = 100 aur prev = 90, toh code 100 se BADA pichla high (jaise 115) pakdega.
    """
    if not swing_highs:
        return False, None, None

    recent_swing_high = swing_highs[-1][1]

    # Recent Swing High se bade saare previous swing highs
    overhead_resistances = [sh[1] for sh in swing_highs[:-1] if sh[1] > recent_swing_high]

    if not overhead_resistances:
        # Open Sky / All-Time High -> Condition Pass
        return True, recent_swing_high, None

    nearest_overhead_resistance = min(overhead_resistances)
    diff_pct = ((nearest_overhead_resistance - recent_swing_high) / recent_swing_high) * 100

    if diff_pct >= MIN_ROOM_TO_RES_PCT:
        return True, recent_swing_high, nearest_overhead_resistance

    return False, recent_swing_high, nearest_overhead_resistance


# ===== MAIN RUNNER =====
stocks = get_watchlist_stocks()

sheet1_data = []  # Step 1 & 2: Liquid + Valid Resistance Distance Stocks
sheet2_data = []  # Step 3: Volume Spike Intact Candidates
sheet3_data = []  # Step 4: Active Status & Entry Distance Tracker

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        # ----------------------------------------------------
        # STEP 1: LIQUIDITY FILTER
        # ----------------------------------------------------
        if not is_liquid_stock(df):
            continue

        view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'

        # ----------------------------------------------------
        # STEP 2: ZIGZAG SWINGS & Overhead Resistance >= 10%
        # ----------------------------------------------------
        curr_highs, curr_lows = get_zigzag_swings(df)
        is_res_valid, recent_entry, overhead_res = check_resistance_distance(curr_highs)

        if is_res_valid and curr_lows:
            recent_sl = curr_lows[-1][1]
            sheet1_data.append({
                "Stock": symbol_clean,
                "Current_Close": round(df.iloc[-1]["Close"], 2),
                "Recent_Swing_High": round(recent_entry, 2),
                "Overhead_Resistance": round(overhead_res, 2) if overhead_res else "Open Sky",
                "Stop_Loss": round(recent_sl, 2),
                "View_Chart": view_chart_link
            })

        # ----------------------------------------------------
        # STEP 3: PREVIOUS 10 DAYS VOLUME SPIKE & INTACT SWING
        # ----------------------------------------------------
        last_10_df = df.iloc[-10:]

        for idx, row in last_10_df.iterrows():
            spike_idx = df.index.get_loc(idx)
            if spike_idx < 20:
                continue

            # Historical Cut-off (Volume Spike day tak ke swings)
            df_until_spike = df.iloc[:spike_idx]
            pre_highs, pre_lows = get_zigzag_swings(df_until_spike)

            if not pre_highs or not pre_lows:
                continue

            spike_res_valid, spike_entry_price, _ = check_resistance_distance(pre_highs)
            if not spike_res_valid:
                continue

            spike_sl_price = pre_lows[-1][1]

            # Volume Spike Check (Previous 10 Days Max Volume)
            prev_10_max_vol = df.iloc[spike_idx - 10 : spike_idx]["Volume"].max()
            current_vol = row["Volume"]

            # Volume Spike + Swing High Intact (Spike High < Recent Swing High)
            if prev_10_max_vol > 0 and current_vol > prev_10_max_vol and row["High"] < spike_entry_price:
                
                spike_date = idx.strftime("%Y-%m-%d")

                sheet2_data.append({
                    "Spike_Date": spike_date,
                    "Stock": symbol_clean,
                    "Recent_Swing_High": round(spike_entry_price, 2),
                    "Stop_Loss": round(spike_sl_price, 2),
                    "View_Chart": view_chart_link
                })

                # ----------------------------------------------------
                # STEP 4: VOLUME SPIKE KE BAAD ENTRY STATUS & DISTANCE
                # ----------------------------------------------------
                post_spike_df = df.iloc[spike_idx:]
                max_high_post_spike = post_spike_df["High"].max()
                current_close = df.iloc[-1]["Close"]

                distance_to_entry_pct = round(((spike_entry_price - current_close) / current_close) * 100, 2)

                if max_high_post_spike >= spike_entry_price:
                    status = "🔥 Entry Triggered"
                    distance_str = "0.00% (Triggered)"
                else:
                    status = "🎯 Entry Pending"
                    distance_str = f"{distance_to_entry_pct}% Away"

                sheet3_data.append({
                    "Spike_Date": spike_date,
                    "Stock": symbol_clean,
                    "Status": status,
                    "Current_Price": round(current_close, 2),
                    "Recent_Swing_High": round(spike_entry_price, 2),
                    "Distance_To_Entry": distance_str,
                    "Stop_Loss": round(spike_sl_price, 2),
                    "View_Chart": view_chart_link
                })
                break

    except Exception:
        pass


# ===== UPLOAD TO GOOGLE SHEETS =====
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
    print("✅ System Run Complete! Exact Resistance & Volume Intact Filter Active.", flush=True)
else:
    ws_active_breakouts.update(values=[["No Active Candidates Found"]], range_name="A1")
    
