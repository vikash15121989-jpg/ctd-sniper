import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: TRADINGVIEW EXACT ZIG ZAG (DEV % + PIVOT LEGS) ===", flush=True)

# ===== TRADINGVIEW EXACT INPUTS (FROM YOUR SCREENSHOT) =====
ZIGZAG_DEV_PCT = 0.05   # 5% Reversal
PIVOT_LEGS = 10         # 10 Bars Pivot Legs
MIN_GAP_PCT = 10.0      # Minimum 10% Gap to Resistance
NEAR_ENTRY_THRESHOLD = 0.03 # Within 3% of Entry Price
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

# ===== TRADINGVIEW EXACT ZIG ZAG ALGORITHM =====
def get_tv_exact_zigzag(df, dev_pct=ZIGZAG_DEV_PCT, legs=PIVOT_LEGS):
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)
    
    # Step 1: Pivot Highs & Pivot Lows (Pivot Legs = 10)
    pivots = [] # List of tuples: (index, price, 'H' or 'L')
    for i in range(legs, n - legs):
        # Pivot High Check
        if all(highs[i] >= highs[i - legs : i]) and all(highs[i] >= highs[i + 1 : i + legs + 1]):
            pivots.append((i, highs[i], 'H'))
        # Pivot Low Check
        if all(lows[i] <= lows[i - legs : i]) and all(lows[i] <= lows[i + 1 : i + legs + 1]):
            pivots.append((i, lows[i], 'L'))

    if not pivots:
        return [], []

    # Step 2: Filter Pivots based on Price Reversal (%)
    swing_highs = []
    swing_lows = []
    
    last_type = None
    last_price = None
    last_idx = None

    for idx, price, p_type in pivots:
        if last_type is None:
            last_type = p_type
            last_price = price
            last_idx = idx
            if p_type == 'H':
                swing_highs.append((idx, price))
            else:
                swing_lows.append((idx, price))
            continue

        if p_type == 'H' and last_type == 'L':
            # Check 5% reversal up from last low
            if price >= last_price * (1 + dev_pct):
                swing_highs.append((idx, price))
                last_type = 'H'
                last_price = price
                last_idx = idx
        elif p_type == 'L' and last_type == 'H':
            # Check 5% reversal down from last high
            if price <= last_price * (1 - dev_pct):
                swing_lows.append((idx, price))
                last_type = 'L'
                last_price = price
                last_idx = idx
        elif p_type == 'H' and last_type == 'H':
            # If consecutive Highs, update if higher
            if price > last_price:
                if swing_highs and swing_highs[-1][0] == last_idx:
                    swing_highs.pop()
                swing_highs.append((idx, price))
                last_price = price
                last_idx = idx
        elif p_type == 'L' and last_type == 'L':
            # If consecutive Lows, update if lower
            if price < last_price:
                if swing_lows and swing_lows[-1][0] == last_idx:
                    swing_lows.pop()
                swing_lows.append((idx, price))
                last_price = price
                last_idx = idx

    return swing_highs, swing_lows


# ===== HYBRID COMBINATION (FALLBACK TO PIVOT LEGS IF REVERSAL TOO STRICT) =====
def get_combined_swings(df):
    tv_highs, tv_lows = get_tv_exact_zigzag(df, dev_pct=ZIGZAG_DEV_PCT, legs=PIVOT_LEGS)
    
    # Fallback to Pivot Legs (10) if deviation criteria excludes recent local peak
    if not tv_highs:
        highs = df["High"].values
        lows = df["Low"].values
        legs = PIVOT_LEGS
        for i in range(legs, len(df) - legs):
            if all(highs[i] >= highs[i - legs : i]) and all(highs[i] >= highs[i + 1 : i + legs + 1]):
                tv_highs.append((i, highs[i]))
            if all(lows[i] <= lows[i - legs : i]) and all(lows[i] <= lows[i + 1 : i + legs + 1]):
                tv_lows.append((i, lows[i]))

    return sorted(tv_highs, key=lambda x: x[0]), sorted(tv_lows, key=lambda x: x[0])


# ===== STEP 1: MAX VOLUME & VOLUME DAY HIGH BELOW SWING HIGH =====
def check_volume_and_spike_day_high(df):
    if len(df) < 30:
        return False, None, None, None

    last_10_df = df.iloc[-10:]
    prev_10_max_vol = df.iloc[-20:-10]["Volume"].max()

    swing_highs, _ = get_combined_swings(df)
    if not swing_highs:
        return False, None, None, None

    recent_30_highs = [sh[1] for sh in swing_highs if sh[0] >= len(df) - 40]
    latest_swing_high_price = max(recent_30_highs) if recent_30_highs else swing_highs[-1][1]

    volume_spike_found = False
    spike_date = None
    spike_day_high = None
    spike_idx = None

    for idx, row in last_10_df.iterrows():
        if prev_10_max_vol > 0 and row["Volume"] > prev_10_max_vol:
            volume_spike_found = True
            spike_date = idx.strftime("%Y-%m-%d")
            spike_day_high = row["High"]
            spike_idx = df.index.get_loc(idx)
            break

    if not volume_spike_found:
        return False, None, None, None

    if spike_day_high < latest_swing_high_price:
        return True, spike_date, latest_swing_high_price, spike_idx

    return False, None, None, None


# ===== STEP 2: READY FOR BREAKOUT FILTER =====
def check_ready_for_breakout(df, stock_symbol, spike_date, spike_idx):
    swing_highs, swing_lows = get_combined_swings(df)

    if not swing_highs or not swing_lows:
        return None, None, None, None

    recent_peaks = [sh[1] for sh in swing_highs if sh[0] >= len(df) - 40]
    entry_swing_high = max(recent_peaks) if recent_peaks else swing_highs[-1][1]

    recent_troughs = [sl[1] for sl in swing_lows if sl[0] >= len(df) - 40]
    recent_swing_low = min(recent_troughs) if recent_troughs else swing_lows[-1][1]

    entry_price = round(entry_swing_high, 2)
    stop_loss = round(recent_swing_low, 2)

    if stop_loss >= entry_price:
        return None, None, None, None

    all_highs = [sh[1] for sh in swing_highs]
    larger_previous_highs = [p for p in all_highs if p > entry_swing_high]

    has_10pct_resistance_space = False

    if larger_previous_highs:
        nearest_larger_high = min(larger_previous_highs)
        gap_pct = ((nearest_larger_high - entry_swing_high) / entry_swing_high) * 100
        if gap_pct >= MIN_GAP_PCT:
            has_10pct_resistance_space = True
    else:
        has_10pct_resistance_space = True

    if not has_10pct_resistance_space:
        return None, None, None, None

    view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

    res_dict = {
        "Date": spike_date,
        "Stock": stock_symbol,
        "Entry_Price": entry_price,
        "Stoploss_Price": stop_loss,
        "View_Chart": view_chart_link,
    }

    return res_dict, entry_price, stop_loss, spike_idx


# ===== POSITION SIZING TAB SETUP =====
def setup_position_sizing_tab(ws):
    try:
        ws.clear()
        time.sleep(1)

        layout = [
            [100000, "⬅️ Enter Your Total Capital (A1)"],
            ["APLLTD", "⬅️ Enter Stock Symbol (A2)"],
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
            is_step1_valid, spike_date, _, spike_idx = check_volume_and_spike_day_high(stock_df)

            if is_step1_valid:
                view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'

                step1_results.append({
                    "Volume_Spike_Date": spike_date,
                    "Stock": symbol_clean,
                    "Current_Close": round(stock_df.iloc[-1]["Close"], 2),
                    "View_Chart": view_chart_link,
                })

                breakout_res, entry_price, stop_loss, s_idx = check_ready_for_breakout(
                    stock_df, symbol_clean, spike_date, spike_idx
                )

                if breakout_res:
                    step2_results.append(breakout_res)

                    post_spike_df = stock_df.iloc[s_idx:]
                    current_close = stock_df.iloc[-1]["Close"]
                    max_high_after_spike = post_spike_df["High"].max()

                    status = None
                    if max_high_after_spike >= entry_price:
                        status = "🔥 Entry Triggered"
                    elif current_close >= entry_price * (1 - NEAR_ENTRY_THRESHOLD) and current_close < entry_price:
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
    print("✅ Exact TradingView ZigZag Calculation Complete!", flush=True)
else:
    ws_active_breakouts.update(values=[["No Active Breakout Candidates"]], range_name="A1")

# Refresh Position Sizing
setup_position_sizing_tab(ws_pos_sizing)
        
