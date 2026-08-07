import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: SWING HIGH/LOW ENTRY & SL SCANNER ===", flush=True)

# ===== CONFIGURATION =====
MIN_GAP_PCT = 10.0  # Minimum 10% Gap Between Previous & Recent Swing High
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
ws_ready_tomorrow = get_or_create_sheet("Ready_For_Tomorrow")


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
    """
    Chart par saare Swing Highs aur Swing Lows find karta hai.
    """
    highs = df["High"].values
    lows = df["Low"].values

    swing_highs = []
    swing_lows = []

    for idx in range(window, len(df) - window):
        # Swing High
        if all(highs[idx] > highs[idx - window : idx]) and all(
            highs[idx] > highs[idx + 1 : idx + window + 1]
        ):
            swing_highs.append(highs[idx])

        # Swing Low
        if all(lows[idx] < lows[idx - window : idx]) and all(
            lows[idx] < lows[idx + 1 : idx + window + 1]
        ):
            swing_lows.append(lows[idx])

    return swing_highs, swing_lows


def scan_swing_gap(df, stock_symbol):
    if len(df) < 50:
        return None

    curr_close = df.iloc[-1]["Close"]

    # Step 1: Swing Highs aur Swing Lows Nikaalo
    swing_highs, swing_lows = find_swing_points(df, window=5)

    if not swing_highs or not swing_lows:
        return None

    # Entry = Nearest Swing High, Stop Loss = Nearest Swing Low
    recent_swing_high = swing_highs[-1]
    recent_swing_low = swing_lows[-1]

    # Stop Loss hamesha Entry Price se niche hona chahiye
    if recent_swing_low >= recent_swing_high:
        return None

    # Price abhi Recent Swing High ke paas hona chahiye (-3% se +5% range)
    dist_from_recent_high = (
        (recent_swing_high - curr_close) / recent_swing_high
    ) * 100
    if dist_from_recent_high > 5.0 or dist_from_recent_high < -3.0:
        return None

    # Step 2: Previous Bada Swing High Condition
    larger_previous_highs = [p for p in swing_highs[:-1] if p > recent_swing_high]

    gap_pct = 0.0
    status_tag = ""

    # CONDITION A: Jab Bada Previous Swing High Maujood Ho
    if larger_previous_highs:
        nearest_larger_high = min(larger_previous_highs)
        gap_pct = (
            (nearest_larger_high - recent_swing_high) / recent_swing_high
        ) * 100

        # Gap 10% se chhota hai toh reject kar do
        if gap_pct < MIN_GAP_PCT:
            return None

        status_tag = f"GAP: {round(gap_pct, 2)}%"

    # CONDITION B: Jab Bada Previous Swing High Ho Hi Na (Open Sky / ATH)
    else:
        status_tag = "NO RESISTANCE (OPEN SKY)"

    # Stock ke naam par hi Hyperlink bana diya gaya hai
    stock_hyperlink = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "{stock_symbol}")'
    chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

    # Calculations
    entry_price = round(recent_swing_high, 2)
    stop_loss = round(recent_swing_low, 2)
    risk_pct = round(((entry_price - stop_loss) / entry_price) * 100, 2)

    return {
        "Stock": stock_hyperlink,  # Name is now a clickable link
        "Entry_Price": entry_price,
        "Stop_Loss": stop_loss,
        "Risk_Pct": risk_pct,
        "Current_Close": round(curr_close, 2),
        "Gap_Status": status_tag,
        "Chart": chart_link, # Ek alag chart button column bhi rahega
    }


# ===== MAIN RUNNER =====
stocks = get_watchlist_stocks()
results = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        stock_df = yf.download(
            stock, start=START_DATE, end=END_DATE, progress=False
        )

        if isinstance(stock_df.columns, pd.MultiIndex):
            stock_df.columns = stock_df.columns.get_level_values(0)

        if not stock_df.empty:
            res = scan_swing_gap(stock_df, symbol_clean)
            if res:
                results.append(res)
    except Exception:
        pass

# Upload to Google Sheet
if results:
    df_res = pd.DataFrame(results)
    ws_ready_tomorrow.clear()
    df_json = json.loads(df_res.to_json(orient="split"))
    ws_ready_tomorrow.update(
        values=[df_json["columns"]] + df_json["data"],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )
    print(f"✅ Total {len(results)} stocks found!")
else:
    ws_ready_tomorrow.clear()
    ws_ready_tomorrow.update(
        values=[["No Matching Stocks Found"]], range_name="A1"
    )
    
