import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ===== CONFIGURATION =====
MIN_VOLUME_UNITS = 5_000_000       # Minimum 50 Lakh Volume
MIN_TURNOVER_VALUE = 20_000_000    # Minimum ₹2 Crore Trading Value
ZIGZAG_DEV_PCT = 0.05              # 5% Reversal
PIVOT_LEGS = 10                    # 10 Legs Depth
MIN_DIFF_PCT = 10.0                # Minimum 10% Gap

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

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]]
    return [s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s for s in stocks]


# ===== STEP 1: PURE ZIGZAG SWING EXTENSION =====
def get_zigzag_swings(df, dev_pct=ZIGZAG_DEV_PCT, legs=PIVOT_LEGS):
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)
    
    if n < (legs * 2 + 1):
        return [], []

    pivots = []
    for i in range(legs, n - legs):
        if all(highs[i] > highs[i - j] for j in range(1, legs + 1)) and all(highs[i] >= highs[i + j] for j in range(1, legs + 1)):
            pivots.append((i, highs[i], 'H'))
        if all(lows[i] < lows[i - j] for j in range(1, legs + 1)) and all(lows[i] <= lows[i + j] for j in range(1, legs + 1)):
            pivots.append((i, lows[i], 'L'))

    if not pivots:
        return [], []

    swing_highs = []
    swing_lows = []
    
    for idx, price, p_type in pivots:
        if p_type == 'H':
            swing_highs.append((idx, price))
        else:
            swing_lows.append((idx, price))

    return swing_highs, swing_lows


# ===== STEP 2: AAPKA EXACT RESISTANCE FILTER LOGIC =====
def process_user_resistance_logic(swing_highs):
    """
    1. Recent Swing High ko entry ($H_1$) maano.
    2. Uske pehle wahi Swing High dekho jo $H_1$ se BADA ho ($H_{prev} > H_1$).
    3. Gap >= 10% calculate karo.
    """
    if not swing_highs:
        return False, None, None

    recent_swing_high = swing_highs[-1][1]

    # $H_1$ se BADE pichle saare Swing Highs (True Resistance Levels)
    past_higher_swings = [sh[1] for sh in swing_highs[:-1] if sh[1] > recent_swing_high]

    if not past_higher_swings:
        # Piche koi bada high nahi mila (Open Sky / ATH)
        return True, recent_swing_high, None

    # $H_1$ se pehla/sabse paas bada resistance high
    true_resistance_high = min(past_higher_swings)

    # Difference Calculation
    diff_pct = ((true_resistance_high - recent_swing_high) / recent_swing_high) * 100.0

    if diff_pct >= MIN_DIFF_PCT:
        return True, recent_swing_high, true_resistance_high

    return False, recent_swing_high, true_resistance_high


# ===== MAIN RUNNER =====
stocks = get_watchlist_stocks()

sheet1_data = []
sheet2_data = []
sheet3_data = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        # 1. LIQUIDITY FILTER
        latest_vol = df.iloc[-1]["Volume"]
        latest_turnover = latest_vol * df.iloc[-1]["Close"]
        if not ((latest_vol >= MIN_VOLUME_UNITS) or (latest_turnover >= MIN_TURNOVER_VALUE)):
            continue

        view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'

        # 2. SHEET 1: RESISTANCE FILTER
        curr_highs, curr_lows = get_zigzag_swings(df)
        is_valid_res, h1_price, h_prev_price = process_user_resistance_logic(curr_highs)

        if is_valid_res and curr_lows:
            sheet1_data.append({
                "Stock": symbol_clean,
                "Current_Close": round(df.iloc[-1]["Close"], 2),
                "Recent_Swing_High_H1": round(h1_price, 2),
                "Resistance_High_Hprev": round(h_prev_price, 2) if h_prev_price else "Open Sky",
                "Stop_Loss": round(curr_lows[-1][1], 2),
                "View_Chart": view_chart_link
            })

        # 3. SHEET 2 & 3: VOLUME SPIKE + SWING INTACT LOGIC
        last_10_df = df.iloc[-10:]

        for idx, row in last_10_df.iterrows():
            spike_idx = df.index.get_loc(idx)
            if spike_idx < 20:
                continue

            prev_10_max_vol = df.iloc[spike_idx - 10 : spike_idx]["Volume"].max()
            current_vol = row["Volume"]

            # Condition A: Volume > Prev 10 Days Max Volume
            if prev_10_max_vol > 0 and current_vol > prev_10_max_vol:
                
                df_until_spike = df.iloc[:spike_idx]
                sp_highs, sp_lows = get_zigzag_swings(df_until_spike)

                if not sp_highs or not sp_lows:
                    continue

                sp_valid, sp_h1, sp_hprev = process_user_resistance_logic(sp_highs)

                # Condition B: Resistance Gap >= 10% AND Spike Day High < H1 (Swing High Intact)
                if sp_valid and row["High"] < sp_h1:
                    spike_date = idx.strftime("%Y-%m-%d")

                    # SHEET 2 ENTRY
                    sheet2_data.append({
                        "Spike_Date": spike_date,
                        "Stock": symbol_clean,
                        "Recent_Swing_High_H1": round(sp_h1, 2),
                        "Resistance_High_Hprev": round(sp_hprev, 2) if sp_hprev else "Open Sky",
                        "Stop_Loss": round(sp_lows[-1][1], 2),
                        "View_Chart": view_chart_link
                    })

                    # SHEET 3 ENTRY (STATUS & ENTRY DISTANCE)
                    post_spike_df = df.iloc[spike_idx:]
                    current_close = df.iloc[-1]["Close"]
                    dist_to_h1_pct = round(((sp_h1 - current_close) / current_close) * 100, 2)

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
                        "Stop_Loss": round(sp_lows[-1][1], 2),
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
    print("✅ Logic Execution Complete!", flush=True)
else:
    ws_active_breakouts.update(values=[["No Active Candidates Found"]], range_name="A1")
    
