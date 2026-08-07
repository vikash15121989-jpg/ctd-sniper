import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: MINIMUM 10% ROOM TO RESISTANCE FILTER ===", flush=True)

# ===== CONFIGURATION =====
ZIGZAG_DEV_PCT = 0.05       # 5% Reversal
PIVOT_LEGS = 10             # 10 Legs
MIN_ROOM_TO_RES_PCT = 10.0  # MINIMUM 10% CLEAR ROOM TO OVERHEAD RESISTANCE
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
ws_vol_filtered = get_or_create_sheet("Volume_Breakout_Watchlist")
ws_ready_for_breakout = get_or_create_sheet("Ready_For_Breakout")
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


# ===== STEP 1: VOLUME SPIKE & 10%+ ROOM TO RESISTANCE CHECK =====
def check_volume_and_distance(df):
    if len(df) < 40:
        return False, None, None, None, None

    swing_highs, swing_lows = get_zigzag_swings(df)
    if not swing_highs or not swing_lows:
        return False, None, None, None, None

    last_10_df = df.iloc[-10:]
    prev_10_max_vol = df.iloc[-20:-10]["Volume"].max()

    for idx, row in last_10_df.iterrows():
        if prev_10_max_vol > 0 and row["Volume"] > prev_10_max_vol:
            spike_date = idx.strftime("%Y-%m-%d")
            spike_day_high = row["High"]
            spike_idx = df.index.get_loc(idx)

            pre_spike_highs = [sh for sh in swing_highs if sh[0] < spike_idx]
            pre_spike_lows = [sl for sl in swing_lows if sl[0] < spike_idx]

            if not pre_spike_highs or not pre_spike_lows:
                continue

            entry_price = pre_spike_highs[-1][1]  # Recent Swing High = Entry Level
            stop_loss = pre_spike_lows[-1][1]    # Recent Swing Low = Stop Loss

            # Find all overhead swing resistances above Entry Price
            overhead_resistances = [sh[1] for sh in pre_spike_highs if sh[1] > entry_price]

            if overhead_resistances:
                nearest_resistance = min(overhead_resistances)
                room_pct = ((nearest_resistance - entry_price) / entry_price) * 100

                # Reject if resistance is closer than 10%
                if room_pct < MIN_ROOM_TO_RES_PCT:
                    continue

            # Volume Day High must be below Entry Price
            if spike_day_high < entry_price:
                return True, spike_date, entry_price, stop_loss, spike_idx

    return False, None, None, None, None


# ===== STEP 2: READY FOR BREAKOUT FILTER =====
def check_ready_for_breakout(df, stock_symbol, spike_date, entry_price, stop_loss, spike_idx):
    entry_price = round(entry_price, 2)
    stop_loss = round(stop_loss, 2)

    if stop_loss >= entry_price:
        return None

    # Secondary safety check for historical chart resistance
    all_past_highs = df.iloc[:spike_idx]["High"].values
    larger_highs = [h for h in all_past_highs if h > entry_price]

    if larger_highs:
        nearest_larger = min(larger_highs)
        gap_pct = ((nearest_larger - entry_price) / entry_price) * 100
        if gap_pct < MIN_ROOM_TO_RES_PCT:
            return None

    view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

    return {
        "Date": spike_date,
        "Stock": stock_symbol,
        "Entry_Price": entry_price,
        "Stoploss_Price": stop_loss,
        "View_Chart": view_chart_link,
    }


# ===== POSITION SIZING TAB SETUP =====
def setup_position_sizing_tab(ws):
    try:
        ws.clear()
        time.sleep(1)

        layout = [
            [100000, "⬅️ Enter Your Total Capital (A1)"],
            ["STOCK_NAME", "⬅️ Enter Stock Symbol (A2)"],
            ["", ""],
            ["Metric / Detail", "Value / Calculation"],
            ["Max Allowed Risk (2% of Capital)", "=A1*0.02"],
            [
                "Entry Price (₹)",
                '=IFERROR(XLOOKUP(UPPER(TRIM(A2)), Ready_For_Breakout!B:B, Ready_For_Breakout!C:C), "Stock Not Found")',
            ],
            [
                "Stop Loss (₹)",
                '=IFERROR(XLOOKUP(UPPER(TRIM(A2)), Ready_For_Breakout!B:B, Ready_For_Breakout!D:D), "Stock Not Found")',
            ],
            ["Risk Per Share (₹)", '=IF(ISNUMBER(B6), B6-B7, "-")'],
            [
                "🎯 BUY POSITION SIZE (QUANTITY)",
                '=IF(AND(ISNUMBER(B8), B8>0), INT(B5/B8), "Invalid Entry/SL")',
            ],
            [
                "Total Investment Amount Needed (₹)",
                '=IF(ISNUMBER(B9), B9*B6, "-")',
            ],
            [
                "Actual Risk Amount (₹)",
                '=IF(ISNUMBER(B9), B9*B8, "-")',
            ],
        ]

        ws.update(values=layout, range_name="A1", value_input_option="USER_ENTERED")
        print("✅ Position Sizing Tab refreshed!", flush=True)
    except Exception as e:
        print(f"Sheet Error [Position_Sizing]: {str(e)}", flush=True)


# ===== MAIN RUNNER =====
stocks = get_watchlist_stocks()
step1_results = []
step2_results = []
active_breakout_results = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        stock_df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False)

        if isinstance(stock_df.columns, pd.MultiIndex):
            stock_df.columns = stock_df.columns.get_level_values(0)

        if not stock_df.empty:
            is_valid, spike_date, entry_price, stop_loss, spike_idx = check_volume_and_distance(stock_df)

            if is_valid:
                view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'

                step1_results.append({
                    "Volume_Spike_Date": spike_date,
                    "Stock": symbol_clean,
                    "Current_Close": round(stock_df.iloc[-1]["Close"], 2),
                    "View_Chart": view_chart_link,
                })

                breakout_res = check_ready_for_breakout(
                    stock_df, symbol_clean, spike_date, entry_price, stop_loss, spike_idx
                )

                if breakout_res:
                    step2_results.append(breakout_res)

                    post_spike_df = stock_df.iloc[spike_idx:]
                    current_close = stock_df.iloc[-1]["Close"]
                    max_high_after_spike = post_spike_df["High"].max()

                    status = None
                    if max_high_after_spike >= entry_price:
                        status = "🔥 Entry Triggered"
                    elif current_close >= entry_price * 0.97 and current_close < entry_price:
                        status = "🎯 Near Entry (Within 3%)"

                    if status:
                        active_breakout_results.append({
                            "Spike_Date": spike_date,
                            "Stock": symbol_clean,
                            "Status": status,
                            "Entry_Price": entry_price,
                            "Current_Price": round(current_close, 2),
                            "Stoploss_Price": stop_loss,
                            "View_Chart": view_chart_link,
                        })

    except Exception:
        pass

# Upload Step 1 Data
ws_vol_filtered.clear()
if step1_results:
    df_step1 = pd.DataFrame(step1_results).sort_values(by="Volume_Spike_Date", ascending=False).reset_index(drop=True)
    json_s1 = json.loads(df_step1.to_json(orient="split"))
    ws_vol_filtered.update(
        values=[json_s1["columns"]] + json_s1["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

# Upload Step 2 Data
ws_ready_for_breakout.clear()
if step2_results:
    df_step2 = pd.DataFrame(step2_results).sort_values(by="Date", ascending=False).reset_index(drop=True)
    json_s2 = json.loads(df_step2.to_json(orient="split"))
    ws_ready_for_breakout.update(
        values=[json_s2["columns"]] + json_s2["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

# Upload Active Breakouts Data
ws_active_breakouts.clear()
if active_breakout_results:
    df_active = pd.DataFrame(active_breakout_results).sort_values(by="Spike_Date", ascending=False).reset_index(drop=True)
    json_active = json.loads(df_active.to_json(orient="split"))
    ws_active_breakouts.update(
        values=[json_active["columns"]] + json_active["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )
    print("✅ System Run Complete! 10%+ Resistance Distance Logic Active.", flush=True)
else:
    ws_active_breakouts.update(values=[["No Active Breakout Candidates"]], range_name="A1")

# Refresh Position Sizing
setup_position_sizing_tab(ws_pos_sizing)
