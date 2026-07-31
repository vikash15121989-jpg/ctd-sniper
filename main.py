import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== CTD SNIPER: TWO-TAB DAILY DUAL SCANNER ===", flush=True)
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

    # 1. Mother Candle (Peak High in 60-day window)
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

    # Condition Check
    curr_close = df.iloc[i]["Close"]

    # Tab 1 Condition: Mother Candle Formed, Dry Up done, BUT Price is strictly BELOW Mother High (Not broken yet)
    is_under_mother = curr_close < mother_high

    if is_under_mother:
        risk_pct = (mother_high - swing_low_price) / mother_high

        # Base Risk Check
        if risk_pct > 0.18 or risk_pct <= 0.01:
            return None, None

        distance_from_high_pct = round(
            ((mother_high - curr_close) / mother_high) * 100, 2
        )

        base_info = {
            "Stock": stock_symbol,
            "Mother_High": round(mother_high, 2),
            "Mother_Date": mother_date,
            "Swing_Low (SL)": round(swing_low_price, 2),
            "Current_Close": round(curr_close, 2),
            "Distance_To_Breakout_Pct": distance_from_high_pct,
            "Est_Risk_Pct": round(risk_pct * 100, 2),
        }

        # Tab 2 Filter: Close is within 2.5% of Mother High (Ready for Tomorrow Breakout)
        if distance_from_high_pct <= 2.5:
            return base_info, base_info  # In both list & ready list
        else:
            return base_info, None  # Only in Mother Candle Watchlist

    return None, None


def upload_to_sheet(ws, data_list, sheet_name):
    try:
        ws.clear()
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)
            df_json = json.loads(df.to_json(orient="split"))
            ws.update(
                values=[df_json["columns"]] + df_json["data"], range_name="A1"
            )
            print(
                f"✅ Uploaded {len(data_list)} stocks to [{sheet_name}]",
                flush=True,
            )
        else:
            ws.update(
                values=[["No Active Candidates Found"]], range_name="A1"
            )
            print(f"ℹ️ No candidate for [{sheet_name}] today.", flush=True)
    except Exception as e:
        print(f"Sheet Error [{sheet_name}]: {str(e)}", flush=True)


# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
mother_watchlist = []
ready_tomorrow_list = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

print(
    f"Scanning {len(stocks)} stocks for Mother Candle & Ready-For-Tomorrow Status...\n",
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
                print(
                    f"🔥 READY FOR TOMORROW: {symbol_clean} | Close: {r_info['Current_Close']} | Breakout Level: {r_info['Mother_High']}"
                )
    except Exception:
        pass

# Upload both tabs
upload_to_sheet(ws_mother_list, mother_watchlist, "Mother_Candle_Watchlist")
upload_to_sheet(ws_ready_tomorrow, ready_tomorrow_list, "Ready_For_Tomorrow")
