import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: FULL SCANNER SYSTEM ===", flush=True)
print(f"Scan Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
TARGET_PCT = 0.10  # 10% Target
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

    i = total_rows - 1

    # 1. Mother Candle Identification
    lookback_window = df.iloc[i - 60 : i]
    mother_idx_loc = lookback_window["High"].idxmax()
    mother_idx = df.index.get_loc(mother_idx_loc)

    if (i - mother_idx) < 5:
        return None, None

    mother_high = df.iloc[mother_idx]["High"]
    mother_vol = df.iloc[mother_idx]["Volume"]

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

    curr_open = df.iloc[i]["Open"]
    curr_high = df.iloc[i]["High"]
    curr_low = df.iloc[i]["Low"]
    curr_close = df.iloc[i]["Close"]
    curr_vol = df.iloc[i]["Volume"]

    # 20-Day Average Volume
    avg_20_vol = df.iloc[i - 20 : i]["Volume"].mean()
    vol_spike_ratio = (
        round(curr_vol / avg_20_vol, 2) if avg_20_vol > 0 else 1.0
    )

    # 5-Day Momentum %
    close_5days_ago = df.iloc[i - 5]["Close"]
    momentum_5d = (
        ((curr_close - close_5days_ago) / close_5days_ago) * 100
        if close_5days_ago > 0
        else 0
    )

    # Candle Quality Filters
    wick_rejection_pct = (
        ((curr_high - curr_close) / curr_high) * 100 if curr_high > 0 else 100
    )
    day_range = curr_high - curr_low
    close_location = (
        ((curr_close - curr_low) / day_range) if day_range > 0 else 0
    )

    upper_threshold = mother_high * 1.05

    if curr_close <= upper_threshold:
        entry_price = round(mother_high, 2)
        stop_loss = round(swing_low_price, 2)
        target_price = round(entry_price * (1 + TARGET_PCT), 2)
        risk_pct = (entry_price - stop_loss) / entry_price

        if risk_pct > 0.18 or risk_pct <= 0.01:
            return None, None

        distance_from_high_pct = round(
            ((mother_high - curr_close) / mother_high) * 100, 2
        )

        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{stock_symbol}", "📈 View Chart")'

        is_high_quality = (
            (wick_rejection_pct <= 2.2)
            and (close_location >= 0.65)
            and (curr_close >= curr_open)
            and (vol_spike_ratio >= 1.3)
        )

        base_info = {
            "Stock": stock_symbol,
            "Entry_Price": entry_price,
            "Stop_Loss": stop_loss,
            "Target_Price": target_price,
            "Current_Close": round(curr_close, 2),
            "Distance_Pct": distance_from_high_pct,
            "Risk_Pct": round(risk_pct * 100, 2),
            "Vol_Spike_Ratio": vol_spike_ratio,
            "Momentum_5D": round(momentum_5d, 2),
            "Is_High_Quality": is_high_quality,
            "Chart": tv_link,
        }

        # 5% Distance Cutoff for Ready_For_Tomorrow
        if distance_from_high_pct <= 5.0:
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

            df["Capped_Vol_Spike"] = df["Vol_Spike_Ratio"].clip(upper=5.0)
            df["Score"] = (
                df["Capped_Vol_Spike"]
                * (df["Momentum_5D"].clip(lower=0.1))
                * df["Is_High_Quality"].astype(int)
            )

            df = df.sort_values(by="Score", ascending=False).reset_index(
                drop=True
            )

            tags = []
            high_prob_count = 0
            for _, row in df.iterrows():
                if row["Is_High_Quality"] and high_prob_count < 5:
                    high_prob_count += 1
                    tags.append(f"🔥 HIGH PROBABILITY #{high_prob_count}")
                else:
                    tags.append("WATCHLIST")

            df["High_Probability_Tag"] = tags
            df["Vol_Spike"] = df["Vol_Spike_Ratio"]

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
                f"✅ Uploaded {len(data_list)} stocks to [Ready_For_Tomorrow]!",
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

            cols_to_drop = [
                col
                for col in [
                    "Vol_Spike_Ratio",
                    "Is_High_Quality",
                    "Momentum_5D",
                ]
                if col in df.columns
            ]
            if cols_to_drop:
                df.drop(columns=cols_to_drop, inplace=True)

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
    try:
        ws.clear()
        time.sleep(1)

        layout = [
            [100000, "⬅️ Enter Your Total Capital (A1)"],
            ["TCS", "⬅️ Enter Stock Symbol (A2)"],
            ["", ""],
            ["Metric / Detail", "Value / Calculation"],
            ["Max Allowed Risk (2% of Capital)", "=A1*0.02"],
            [
                "Entry Price (₹)",
                '=IFERROR(XLOOKUP(UPPER(TRIM(A2)), Ready_For_Tomorrow!A:A, Ready_For_Tomorrow!B:B), "Stock Not Found")',
            ],
            [
                "Stop Loss (₹)",
                '=IFERROR(XLOOKUP(UPPER(TRIM(A2)), Ready_For_Tomorrow!A:A, Ready_For_Tomorrow!C:C), "Stock Not Found")',
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

        ws.update(
            values=layout, range_name="A1", value_input_option="USER_ENTERED"
        )
        print("✅ Position Sizing Tab refreshed!", flush=True)
    except Exception as e:
        print(f"Sheet Error [Position_Sizing]: {str(e)}", flush=True)


# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
mother_watchlist = []
ready_tomorrow_list = []

# Exclude Non-Stock Keywords
REJECT_KEYWORDS = [
    "LIQUID",
    "ETF",
    "CPSE",
    "NETF",
    "GILT",
    "GOLD",
    "SILVER",
    "BEES",
    "NEXT50",
    "BETA",
    "INDEX",
    "IADD",
    "CASE",
    "MOQUALITY",
    "MOLOWVOL",
]

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

upload_to_sheet(ws_mother_list, mother_watchlist, "Mother_Candle_Watchlist")
upload_ranked_ready_sheet(ws_ready_tomorrow, ready_tomorrow_list)
setup_position_sizing_tab(ws_pos_sizing)
    
