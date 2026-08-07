import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: VOLUME SPIKE CANDLE HIGH SCANNER ===", flush=True)

# ===== CONFIGURATION =====
MIN_GAP_PCT = 10.0  # Minimum 10% Gap to Resistance
LOOKBACK_DAYS = 365  # 1 Year Data

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


def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [
        s.strip().upper()
        for s in stocks
        if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]
    ]
    return [
        s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s
        for s in stocks
    ]


def find_swing_points(df, window=5):
    highs = df["High"].values
    lows = df["Low"].values

    swing_highs = []
    swing_lows = []

    for idx in range(window, len(df) - window):
        # Swing High
        if all(highs[idx] > highs[idx - window : idx]) and all(
            highs[idx] > highs[idx + 1 : idx + window + 1]
        ):
            swing_highs.append((idx, highs[idx]))

        # Swing Low
        if all(lows[idx] < lows[idx - window : idx]) and all(
            lows[idx] < lows[idx + 1 : idx + window + 1]
        ):
            swing_lows.append((idx, lows[idx]))

    return swing_highs, swing_lows


# ===== STEP 1: MAX VOLUME & VOLUME DAY HIGH BELOW SWING HIGH =====
def check_volume_and_spike_day_high(df):
    if len(df) < 30:
        return False, None, None

    last_10_df = df.iloc[-10:]
    # Previous 10 Days MAX Volume
    prev_10_max_vol = df.iloc[-20:-10]["Volume"].max()

    swing_highs, _ = find_swing_points(df, window=5)
    if not swing_highs:
        return False, None, None

    latest_swing_high_price = swing_highs[-1][1]

    volume_spike_found = False
    spike_date = None
    spike_day_high = None

    # Check if any day in last 10 days had volume > previous 10 days max volume
    for idx, row in last_10_df.iterrows():
        if prev_10_max_vol > 0 and row["Volume"] > prev_10_max_vol:
            volume_spike_found = True
            spike_date = idx.strftime("%Y-%m-%d")
            spike_day_high = row["High"]  # Jis din volume aaya us din ka HIGH
            break

    if not volume_spike_found:
        return False, None, None

    # PRICE CONDITION: Jis din volume aaya us din ka High Price Swing High se niche hona chahiye
    if spike_day_high < latest_swing_high_price:
        return True, spike_date, latest_swing_high_price

    return False, None, None


# ===== STEP 2: RESISTANCE SPACE FILTER =====
def check_ready_for_breakout(df, stock_symbol, spike_date):
    swing_highs, swing_lows = find_swing_points(df, window=5)

    if not swing_highs or not swing_lows:
        return None

    recent_swing_high_price = swing_highs[-1][1]
    recent_swing_low_price = swing_lows[-1][1]

    # Entry = Nearest Swing High, Stop Loss = Nearest Swing Low
    entry_price = round(recent_swing_high_price, 2)
    stop_loss = round(recent_swing_low_price, 2)

    if stop_loss >= entry_price:
        return None

    # Previous Swing Highs for Resistance Check
    all_highs = [sh[1] for sh in swing_highs]
    larger_previous_highs = [p for p in all_highs[:-1] if p > recent_swing_high_price]

    has_10pct_resistance_space = False

    if larger_previous_highs:
        nearest_larger_high = min(larger_previous_highs)
        gap_pct = (
            (nearest_larger_high - recent_swing_high_price) / recent_swing_high_price
        ) * 100
        if gap_pct >= MIN_GAP_PCT:
            has_10pct_resistance_space = True
    else:
        # Overhead Resistance Hi Nahi Hai (Open Sky / ATH Zone)
        has_10pct_resistance_space = True

    if not has_10pct_resistance_space:
        return None

    stock_hyperlink = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "{stock_symbol}")'
    view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

    return {
        "Date": spike_date,
        "Stock": stock_hyperlink,
        "Entry_Price": entry_price,
        "Stoploss_Price": stop_loss,
        "View_Chart": view_chart_link,
    }


# ===== MAIN RUNNER =====
stocks = get_watchlist_stocks()
step1_results = []
step2_results = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        stock_df = yf.download(
            stock, start=START_DATE, end=END_DATE, progress=False
        )

        if isinstance(stock_df.columns, pd.MultiIndex):
            stock_df.columns = stock_df.columns.get_level_values(0)

        if not stock_df.empty:
            # Step 1 Check
            is_step1_valid, spike_date, _ = check_volume_and_spike_day_high(stock_df)

            if is_step1_valid:
                view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "📈 View Chart")'
                stock_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol_clean}", "{symbol_clean}")'

                step1_results.append({
                    "Volume_Spike_Date": spike_date,
                    "Stock": stock_link,
                    "Current_Close": round(stock_df.iloc[-1]["Close"], 2),
                    "View_Chart": view_chart_link,
                })

                # Step 2 Check (Filter from Step 1)
                breakout_res = check_ready_for_breakout(stock_df, symbol_clean, spike_date)
                if breakout_res:
                    step2_results.append(breakout_res)

    except Exception:
        pass

# Upload Step 1 Data to [Volume_Breakout_Watchlist]
ws_vol_filtered.clear()
if step1_results:
    df_step1 = pd.DataFrame(step1_results)
    json_s1 = json.loads(df_step1.to_json(orient="split"))
    ws_vol_filtered.update(
        values=[json_s1["columns"]] + json_s1["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )
else:
    ws_vol_filtered.update(values=[["No Volume Breakout Candidates"]], range_name="A1")

# Upload Step 2 Data to [Ready_For_Breakout]
ws_ready_for_breakout.clear()
if step2_results:
    df_step2 = pd.DataFrame(step2_results)
    json_s2 = json.loads(df_step2.to_json(orient="split"))
    ws_ready_for_breakout.update(
        values=[json_s2["columns"]] + json_s2["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )
    print(f"✅ Filtered {len(step1_results)} stocks in Step 1, {len(step2_results)} stocks in Ready_For_Breakout!")
else:
    ws_ready_for_breakout.update(values=[["No Ready For Breakout Candidates"]], range_name="A1")
    
