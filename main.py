import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print(
    "=== CTD SNIPER: DUAL SCANNER WITH SINGLE-STOCK POSITION CALCULATOR ===",
    flush=True,
)
print(f"Scan Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
TARGET_PCT = 0.10  # 10% Profit Target
LOOKBACK_DAYS = 180

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
ws_mother_list = get_or_create_sheet("Mother_Candle_Watchlist")
ws_ready_tomorrow = get_or_create_sheet("Ready_For_Tomorrow")
ws_pos_sizing = get_or_create_sheet("Position_Sizing")


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


def flatten_yf_columns(df):
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip().capitalize() for col in df.columns]
    if "Close" not in df.columns and "Adj close" in df.columns:
        df["Close"] = df["Adj close"]
    df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    return df


def scan_dual_status(df, stock_symbol):
    total_rows = len(df)
    if total_rows < 70:
        return None, None

    i = total_rows - 1  # Latest candle

    # 1. Mother Candle Identification
    lookback_window = df.iloc[i - 60 : i]
    mother_idx_loc = lookback_window["High"].idxmax()
    mother_idx = df.index.get_loc(mother_idx_loc)

    if (i - mother_idx) < 5:
        return None, None

    mother_high = df.iloc[mother_idx]["High"]
    mother_vol = df.iloc[mother_idx]["Volume"]
    mother_date = df.index[mother_idx].strftime("%Y-%m-%d")

    # 2. Swing Low Identification
    post_mother_zone = df.iloc[mother_idx:i]
    swing_low_idx_loc = post_mother_zone["Low"].idxmin()
    swing_low_idx = df.index.get_loc(swing_low_idx_loc)
    swing_low_price = df.iloc[swing_low_idx]["Low"]

    # 3. Volume Dry-Up Check
    pullback_vols = df.iloc[mother_idx + 1 : swing_low_idx + 1]["Volume"]
    if len(pullback_vols) > 0:
        avg_pullback_vol = pullback_vols.mean()
        is_vol_dry_up = avg_pullback_vol < (0.75 * mother_vol)
    else:
        is_vol_dry_up = False

    if not is_vol_dry_up:
        return None, None

    curr_close = df.iloc[i]["Close"]
    curr_vol = df.iloc[i]["Volume"]

    # 20-Day Average Volume for Spike check
    avg_20_vol = df.iloc[i - 20 : i]["Volume"].mean()
    vol_spike_ratio = (
        round(curr_vol / avg_20_vol, 2) if avg_20_vol > 0 else 1.0
    )

    # Condition: Under Mother High (Breakout not triggered yet)
    is_under_mother = curr_close < mother_high

    if is_under_mother:
        entry_price = round(mother_high, 2)
        stop_loss = round(swing_low_price, 2)
        target_price = round(entry_price * (1 + TARGET_PCT), 2)
        risk_pct = (entry_price - stop_loss) / entry_price

        if risk_pct > 0.18 or risk_pct <= 0.01:
            return None, None

        distance_from_high_pct = round(
            ((mother_high - curr_close) / mother_high) * 100, 2
        )

        # TradingView Chart Formula
        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

        base_info = {
            "Stock": stock_symbol,  # Column A
            "Entry_Price": entry_price,  # Column B
            "Stop_Loss": stop_loss,  # Column C
            "Target_Price": target_price,  # Column D
            "Current_Close": round(curr_close, 2),  # Column E
            "Distance_Pct": distance_from_high_pct,  # Column F
            "Risk_Pct": round(risk_pct * 100, 2),  # Column G
            "Vol_Spike_Ratio": vol_spike_ratio,  # Internal Column
            "Chart": tv_link,  # Column J
        }

        # Ready for Tomorrow Condition (Distance <= 2.5%)
        if distance_from_high_pct <= 2.5:
            return base_info, base_info
        else:
            return base_info, None

    return None, None


def upload_ranked_ready_sheet(ws, data_list):
    try:
        ws.clear()
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)

            # High Probability Sorting (Volume Spike High + Distance Low)
            df["Score"] = df["Vol_Spike_Ratio"] / (df["Distance_Pct"] + 0.1)
            df = df.sort_values(by="Score", ascending=False).reset_index(
                drop=True
            )

            df["Vol_Spike"] = df["Vol_Spike_Ratio"]
            df["High_Probability_Tag"] = [
                f"🔥 HIGH PROBABILITY #{i+1}" if i < 5 else "WATCHLIST"
                for i in range(len(df))
            ]

            ordered_cols = [
                "Stock",
                "Entry_Price",
                "Stop_Loss",
                "Target_Price",
                "Current_Close",
                "Distance_Pct",
                "Risk_Pct",
                "Vol_Spike",
                "High_Probability_Tag",
                "Chart",
            ]
            df = df[ordered_cols]

            df_json = json.loads(df.to_json(orient="split"))
            ws.update(
                values=[df_json["columns"]] + df_json["data"],
                range_name="A1",
                value_input_option="USER_ENTERED",
            )
            print(
                f"✅ Uploaded {len(data_list)} stocks with Chart Links!",
                flush=True,
            )
        else:
            ws.update(
                values=[["No Active Candidates Found"]], range_name="A1"
            )
    except Exception as e:
        print(f"Sheet Error [Ready_For_Tomorrow]: {str(e)}", flush=True)


def upload_to_sheet(ws, data_list, sheet_name):
    try:
        ws.clear()
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)

            if "Vol_Spike_Ratio" in df.columns:
                df.drop(columns=["Vol_Spike_Ratio"], inplace=True)

            ordered_cols = [
                "Stock",
                "Entry_Price",
                "Stop_Loss",
                "Target_Price",
                "Current_Close",
                "Distance_Pct",
                "Risk_Pct",
                "Chart",
            ]
            df = df[ordered_cols]

            df_json = json.loads(df.to_json(orient="split"))
            ws.update(
                values=[df_json["columns"]] + df_json["data"],
                range_name="A1",
                value_input_option="USER_ENTERED",
            )
            print(
                f"✅ Uploaded {len(data_list)} stocks to [{sheet_name}]!",
                flush=True,
            )
        else:
            ws.update(
                values=[["No Active Candidates Found"]], range_name="A1"
            )
    except Exception as e:
        print(f"Sheet Error [{sheet_name}]: {str(e)}", flush=True)


def setup_position_sizing_tab(ws):
    """Configures A1 as Capital Input and A2 as Stock Input with auto calculations"""
    try:
        # Check if setup already exists so we don't wipe out user inputs in A1 & A2
        existing_label = ws.acell("B1").value
        if not existing_label or "TOTAL CAPITAL" not in str(existing_label):
            ws.clear()
            time.sleep(1)

            # Creating Structured Single-Stock Calculator Layout
            layout = [
                [
                    100000,
                    "⬅️ Enter Your Total Capital (A1)",
                ],  # Row 1: A1 = Capital
                [
                    "TATAMOTORS",
                    "⬅️ Enter Stock Symbol (A2)",
                ],  # Row 2: A2 = Stock Symbol
                ["", ""],  # Row 3: Blank
                [
                    "Metric / Detail",
                    "Value / Calculation",
                ],  # Row 4: Table Headers
                [
                    "Max Allowed Risk (2% of Capital)",
                    "=A1*0.02",
                ],  # Row 5: Max Risk (B5)
                [
                    "Entry Price (₹)",
                    '=IFERROR(XLOOKUP(A2, Ready_For_Tomorrow!A:A, Ready_For_Tomorrow!B:B), "Stock Not Found")',
                ],  # Row 6: Entry (B6)
                [
                    "Stop Loss (₹)",
                    '=IFERROR(XLOOKUP(A2, Ready_For_Tomorrow!A:A, Ready_For_Tomorrow!C:C), "Stock Not Found")',
                ],  # Row 7: SL (B7)
                [
                    "Risk Per Share (₹)",
                    '=IF(ISNUMBER(B6), B6-B7, "-")',
                ],  # Row 8: Risk/Share (B8)
                [
                    "🎯 BUY POSITION SIZE (QUANTITY)",
                    '=IF(AND(ISNUMBER(B8), B8>0), INT(B5/B8), "Invalid Entry/SL")',
                ],  # Row 9: Quantity (B9)
                [
                    "Total Investment Amount Needed (₹)",
                    '=IF(ISNUMBER(B9), B9*B6, "-")',
                ],  # Row 10: Investment (B10)
                [
                    "Actual Risk Amount (₹)",
                    '=IF(ISNUMBER(B9), B9*B8, "-")',
                ],  # Row 11: Actual Loss (B11)
            ]

            ws.update(
                values=layout,
                range_name="A1",
                value_input_option="USER_ENTERED",
            )
            print(
                "✅ Created & Configured [Position_Sizing] Tab (A1=Capital, A2=Stock)!",
                flush=True,
            )
    except Exception as e:
        print(f"Sheet Error [Position_Sizing]: {str(e)}", flush=True)


# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
mother_watchlist = []
ready_tomorrow_list = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

print(
    f"Scanning {len(stocks)} stocks for Signals & Setting up Calculator...\n",
    flush=True,
)

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        if any(k in symbol_clean for k in REJECT_KEYWORDS):
            continue

        stock_df = yf.download(
            stock, start=START_DATE, end=END_DATE, progress=False
        )
        stock_df = flatten_yf_columns(stock_df)

        if not stock_df.empty:
            m_info, r_info = scan_dual_status(stock_df, symbol_clean)
            if m_info:
                mother_watchlist.append(m_info)
            if r_info:
                ready_tomorrow_list.append(r_info)
    except Exception:
        pass

# Upload Sheets & Setup Sizing Calculator Tab
upload_to_sheet(ws_mother_list, mother_watchlist, "Mother_Candle_Watchlist")
upload_ranked_ready_sheet(ws_ready_tomorrow, ready_tomorrow_list)
setup_position_sizing_tab(ws_pos_sizing)
