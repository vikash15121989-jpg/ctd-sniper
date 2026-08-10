import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ANNA COULLING COMPLETE VPA SYSTEM ENGINE ===", flush=True)

# ===== CONFIGURATION =====
DEFAULT_CAPITAL = 100000.0         
DEFAULT_RISK_PCT = 1.0            

MIN_VOLUME_UNITS = 100_000         
MIN_TURNOVER_VALUE = 5_000_000     

VOL_MA_LEN = 20
SPREAD_MULT = 1.2
VOL_MULT = 1.5

END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=730) # 2 Years Data for Weekly Analysis

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
ws_master_setups = get_or_create_sheet("Anna_VPA_Breakout_Retest")
ws_single_signals = get_or_create_sheet("Anna_VPA_Single_Signals")
ws_sr_zones = get_or_create_sheet("Anna_VPA_SR_Zones")
ws_position_sizing = get_or_create_sheet("Position_Sizing")

# Read Capital from Sheet Cell A1
try:
    user_capital_val = ws_position_sizing.acell("A1").value
    if user_capital_val:
        clean_val = str(user_capital_val).replace("₹", "").replace(",", "").strip()
        TOTAL_CAPITAL = float(clean_val)
    else:
        TOTAL_CAPITAL = DEFAULT_CAPITAL
except Exception:
    TOTAL_CAPITAL = DEFAULT_CAPITAL

MAX_RISK_AMOUNT = TOTAL_CAPITAL * (DEFAULT_RISK_PCT / 100.0)

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]]
    return [s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s for s in stocks]

# ===== AUTOMATED SUPPORT & RESISTANCE FINDER =====
def find_support_resistance(df, window=10):
    df_sr = df.copy()
    df_sr['Pivot_High'] = df_sr['High'][(df_sr['High'] == df_sr['High'].rolling(window*2+1, center=True).max())]
    df_sr['Pivot_Low'] = df_sr['Low'][(df_sr['Low'] == df_sr['Low'].rolling(window*2+1, center=True).min())]
    
    recent_resistance = df_sr['Pivot_High'].dropna().iloc[-3:].mean() if not df_sr['Pivot_High'].dropna().empty else df['High'].max()
    recent_support = df_sr['Pivot_Low'].dropna().iloc[-3:].mean() if not df_sr['Pivot_Low'].dropna().empty else df['Low'].min()
    
    return round(recent_support, 2), round(recent_resistance, 2)

# ===== FULL ANNA COULLING VPA ENGINE =====
def run_full_anna_coulling_vpa(df_daily, symbol):
    # ----------------------------------------------------
    # 1. WEEKLY TIMEFRAME (Top-Down Trend Validation)
    # ----------------------------------------------------
    df_weekly = df_daily.resample('W').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    }).dropna()

    weekly_trend = "NEUTRAL"
    if len(df_weekly) >= 20:
        df_weekly['Vol_MA'] = df_weekly['Volume'].rolling(20).mean()
        df_weekly['Spread'] = df_weekly['High'] - df_weekly['Low']
        df_weekly['Spread_MA'] = df_weekly['Spread'].rolling(20).mean()

        last_4_w = df_weekly.iloc[-4:]
        w_green_vol = last_4_w[(last_4_w['Close'] > last_4_w['Open']) & (last_4_w['Volume'] > last_4_w['Vol_MA'])]
        if len(w_green_vol) >= 2:
            weekly_trend = "BULLISH"

    # ----------------------------------------------------
    # 2. DAILY TIMEFRAME CALCULATIONS & ANOMALIES
    # ----------------------------------------------------
    df = df_daily.copy()
    df['Vol_MA'] = df['Volume'].rolling(VOL_MA_LEN).mean()
    df['Spread'] = df['High'] - df['Low']
    df['Spread_MA'] = df['Spread'].rolling(VOL_MA_LEN).mean()

    df['Is_Wide_Spread'] = df['Spread'] > (df['Spread_MA'] * SPREAD_MULT)
    df['Is_Narrow_Spread'] = df['Spread'] < (df['Spread_MA'] / SPREAD_MULT)
    df['Is_High_Vol'] = df['Volume'] > (df['Vol_MA'] * VOL_MULT)
    df['Is_Low_Vol'] = df['Volume'] < (df['Vol_MA'] / VOL_MULT)
    df['Is_Ultra_High_Vol'] = df['Volume'] > (df['Vol_MA'] * 2.25)

    df['Is_Green'] = df['Close'] > df['Open']
    df['Is_Red'] = df['Close'] < df['Open']
    df['Lower_Wick'] = np.minimum(df['Open'], df['Close']) - df['Low']
    df['Upper_Wick'] = df['High'] - np.maximum(df['Open'], df['Close'])
    df['Has_Lower_Wick'] = df['Lower_Wick'] > (df['Spread'] * 0.35)
    df['Has_Upper_Wick'] = df['Upper_Wick'] > (df['Spread'] * 0.35)

    # Anna Coulling Single-Bar VPA Conditions
    df['Real_Breakout'] = df['Is_Green'] & df['Is_Wide_Spread'] & df['Is_High_Vol']
    df['Fake_Breakout'] = df['Is_Green'] & df['Is_Wide_Spread'] & df['Is_Low_Vol']
    
    df['Highest_10'] = df['High'].rolling(10).max()
    df['Buying_Climax'] = df['Is_Ultra_High_Vol'] & (df['Is_Narrow_Spread'] | df['Has_Upper_Wick']) & (df['High'] == df['Highest_10'])

    df['Lowest_10'] = df['Low'].rolling(10).min()
    df['Selling_Climax'] = df['Is_Ultra_High_Vol'] & (df['Is_Narrow_Spread'] | df['Has_Lower_Wick']) & (df['Low'] == df['Lowest_10'])

    df['No_Supply_Test'] = df['Is_Red'] & df['Is_Low_Vol'] & df['Has_Lower_Wick']
    df['No_Demand_Test'] = df['Is_Green'] & df['Is_Low_Vol'] & df['Has_Upper_Wick']

    view_chart_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol}", "📈 View Chart")'

    # Support & Resistance Zones
    sup_level, res_level = find_support_resistance(df)

    # ----------------------------------------------------
    # 3. SINGLE BAR SIGNALS COLLECTION
    # ----------------------------------------------------
    single_signals = []
    last_10 = df.iloc[-10:]
    for idx, row in last_10.iterrows():
        sig_date = idx.strftime("%Y-%m-%d")
        close_px = round(row["Close"], 2)
        vol = int(row["Volume"])
        sl = round(row["Low"], 2)

        sig_list = []
        if row['Real_Breakout']: sig_list.append("🔥 High-Vol Breakout")
        if row['Fake_Breakout']: sig_list.append("⚠️ Low-Vol Fakeout Trap")
        if row['Buying_Climax']: sig_list.append("🔴 Buying Climax (Top)")
        if row['Selling_Climax']: sig_list.append("🟢 Selling Climax / Stopping Vol")
        if row['No_Supply_Test']: sig_list.append("🎯 No Supply Test")
        if row['No_Demand_Test']: sig_list.append("📉 No Demand Test")

        if sig_list:
            single_signals.append({
                "Date": sig_date, "Stock": symbol,
                "VPA_Signal": ", ".join(sig_list),
                "Close": close_px, "Volume": vol, "Stop_Loss": sl,
                "View_Chart": view_chart_link
            })

    # ----------------------------------------------------
    # 4. MULTI-BAR BREAKOUT + LOW VOL RETEST PATTERN
    # ----------------------------------------------------
    master_retest_setup = None
    n_daily = len(df)
    
    # Check last 15 days for a breakout
    for bo_i in range(n_daily - 15, n_daily - 2):
        if df['Real_Breakout'].iloc[bo_i]:
            bo_row = df.iloc[bo_i]
            bo_date = df.index[bo_i].strftime("%Y-%m-%d")
            bo_high = bo_row['High']
            bo_low = bo_row['Low']
            bo_vol_ma = bo_row['Vol_MA']

            retest_phase = df.iloc[bo_i + 1 :]
            if len(retest_phase) < 2: continue

            min_retest_low = retest_phase['Low'].min()
            latest_close = df.iloc[-1]['Close']
            avg_retest_vol = retest_phase['Volume'].mean()

            # Anna Retest Conditions: Holding Breakout/Resistance Zone + Low Volume Drop
            is_holding_zone = (min_retest_low <= max(bo_high, res_level) * 1.02) and (latest_close >= bo_low)
            is_vol_dry = avg_retest_vol < (bo_vol_ma * 0.80)

            if is_holding_zone and is_vol_dry:
                sl_price = round(min_retest_low, 2)
                entry_price = round(latest_close, 2)
                vol_drop = round((1 - (avg_retest_vol / bo_vol_ma)) * 100, 1)

                master_retest_setup = {
                    "Breakout_Date": bo_date,
                    "Stock": symbol,
                    "Weekly_Trend": f"🟢 {weekly_trend}" if weekly_trend == "BULLISH" else "🟡 NEUTRAL",
                    "Resistance_Zone": res_level,
                    "Entry_Price": entry_price,
                    "Stop_Loss": sl_price,
                    "Retest_Vol_Drop": f"{vol_drop}%",
                    "View_Chart": view_chart_link
                }
                break

    sr_info = {
        "Stock": symbol,
        "Support_Zone": sup_level,
        "Resistance_Zone": res_level,
        "Current_Close": round(df.iloc[-1]['Close'], 2),
        "Weekly_Trend": weekly_trend,
        "View_Chart": view_chart_link
    }

    return master_retest_setup, single_signals, sr_info

# ===== MAIN EXECUTION PIPELINE =====
stocks = get_watchlist_stocks()

master_setups = []
master_single_signals = []
master_sr_zones = []
position_sizing_data = []

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100: continue

        latest_vol = df.iloc[-1]["Volume"]
        latest_turnover = latest_vol * df.iloc[-1]["Close"]
        if not ((latest_vol >= MIN_VOLUME_UNITS) or (latest_turnover >= MIN_TURNOVER_VALUE)):
            continue

        retest_setup, single_sigs, sr_info = run_full_anna_coulling_vpa(df, symbol_clean)

        master_single_signals.extend(single_sigs)
        master_sr_zones.append(sr_info)

        if retest_setup:
            master_setups.append(retest_setup)

            # Position Sizing Logic (1% Risk Management)
            entry_px = retest_setup["Entry_Price"]
            sl_px = retest_setup["Stop_Loss"]
            risk_per_share = entry_px - sl_px

            if risk_per_share > 0:
                qty = int(MAX_RISK_AMOUNT / risk_per_share)
                total_inv = round(qty * entry_px, 2)

                position_sizing_data.append({
                    "Breakout_Date": retest_setup["Breakout_Date"],
                    "Stock": symbol_clean,
                    "Pattern": "Weekly Sync + Low-Vol Retest Entry",
                    "Entry_Price": entry_px,
                    "Stop_Loss": sl_px,
                    "Risk_Per_Share": round(risk_per_share, 2),
                    "Qty_To_Buy": qty,
                    "Total_Investment": f"₹{total_inv:,.2f}",
                    "Max_Risk_1%": f"₹{int(MAX_RISK_AMOUNT):,}",
                    "View_Chart": retest_setup["View_Chart"]
                })

    except Exception:
        pass

# ===== UPLOAD TO GOOGLE SHEETS =====
ws_master_setups.clear()
if master_setups:
    df_ms = pd.DataFrame(master_setups).sort_values(by="Breakout_Date", ascending=False)
    json_ms = json.loads(df_ms.to_json(orient="split"))
    ws_master_setups.update(values=[json_ms["columns"]] + json_ms["data"], range_name="A1", value_input_option="USER_ENTERED")

ws_single_signals.clear()
if master_single_signals:
    df_ss = pd.DataFrame(master_single_signals).sort_values(by="Date", ascending=False)
    json_ss = json.loads(df_ss.to_json(orient="split"))
    ws_single_signals.update(values=[json_ss["columns"]] + json_ss["data"], range_name="A1", value_input_option="USER_ENTERED")

ws_sr_zones.clear()
if master_sr_zones:
    df_sr = pd.DataFrame(master_sr_zones)
    json_sr = json.loads(df_sr.to_json(orient="split"))
    ws_sr_zones.update(values=[json_sr["columns"]] + json_sr["data"], range_name="A1", value_input_option="USER_ENTERED")

if position_sizing_data:
    ws_position_sizing.batch_clear(["A4:Z500"])
    headers = [["Breakout_Date", "Stock", "Pattern", "Entry_Price", "Stop_Loss", "Risk_Per_Share", "Qty_To_Buy", "Total_Investment", "Max_Risk_1%", "View_Chart"]]
    ws_position_sizing.update(values=headers, range_name="A3", value_input_option="USER_ENTERED")

    df_ps = pd.DataFrame(position_sizing_data).sort_values(by="Breakout_Date", ascending=False)
    json_ps = json.loads(df_ps.to_json(orient="split"))
    
    ws_position_sizing.update(values=json_ps["data"], range_name="A4", value_input_option="USER_ENTERED")
    print("✅ Full Anna Coulling VPA System Successfully Executed!", flush=True)
else:
    ws_position_sizing.batch_clear(["A4:Z500"])
    ws_position_sizing.update(values=[["No Active High-Conviction Entries Found"]], range_name="A4")
