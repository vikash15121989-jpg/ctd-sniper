from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== [OFF-MARKET] RUNNING EOD SCANNER | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(val) for val in row] for row in rows]

def calculate_gann_levels(price):
    if price <= 0:
        return [], 0.0
    root = math.sqrt(price)
    gann_resistances = [round((root + f)**2, 2) for f in [0.5, 1.0, 1.5, 2.0]]
    next_360_target = round((root + 2.0)**2, 2)
    return gann_resistances, next_360_target

# Connect to Google Sheets
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheet: CTD_Sniper", flush=True)
except Exception as e:
    print(f"❌ Error connecting to Google Sheets: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="12")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]

def is_etf(symbol_name):
    return any(kw in symbol_name.upper() for kw in ETF_KEYWORDS)

# Read Watchlist
try:
    raw_stocks = sh.worksheet("Watchlist").col_values(1)
except Exception as e:
    print(f"❌ Error reading Watchlist: {e}")
    exit(1)

STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))
print(f"📥 Scanning EOD Data for {len(clean_stocks)} stocks...", flush=True)

filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        
        df = yf.Ticker(symbol).history(period="60d", interval="1d")
        if df.empty or len(df) < 35: 
            continue

        df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()

        last_close = float(df['Close'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])

        if math.isnan(avg_vol) or avg_vol < 30000 or math.isnan(last_close): 
            continue  

        df_40d = df.iloc[-40:]
        recent_3d = df.iloc[-3:]

        recent_resistance = float(df_40d['High'].max())
        peak1_idx = df_40d['High'].idxmax()

        df_prior = df_40d.drop(peak1_idx)
        prior_resistance = float(df_prior['High'].max())

        # Logic Rules
        is_cross_or_near = recent_resistance >= (prior_resistance * 0.98)

        breakout_day_vol = float(df_40d.loc[peak1_idx, 'Volume'])
        breakout_day_avg_vol = float(df_40d.loc[peak1_idx, 'Vol_SMA20'])
        is_breakout_day_high_vol = (breakout_day_vol >= 1.3 * breakout_day_avg_vol)

        last_vol = float(recent_3d['Volume'].iloc[-1])
        is_low_vol_pullback = (recent_3d['Volume'] < recent_3d['Vol_SMA20']).sum() >= 1

        is_near_resistance = (last_close >= recent_resistance * 0.93) and (last_close <= recent_resistance * 1.05)

        gann_resistances, next_gann_target = calculate_gann_levels(recent_resistance)
        is_gann_confluence = any(abs(recent_resistance - g_res) / g_res <= 0.02 for g_res in gann_resistances)

        if is_cross_or_near and is_breakout_day_high_vol and is_low_vol_pullback and is_near_resistance:
            trigger_high = round(recent_resistance, 2)
            demand_zone_low = round(float(df['Low'].iloc[-20:].min()), 2)
            dry_ratio = f"{round((last_vol / avg_vol) * 100, 1)}%"

            if is_gann_confluence and last_vol < (avg_vol * 0.7):
                probability_tag = "HIGH PROBABILITY (GANN CONFLUENCE)"
            elif is_gann_confluence:
                probability_tag = "GANN CONFLUENCE"
            elif last_vol < (avg_vol * 0.7):
                probability_tag = "HIGH PROBABILITY"
            else:
                probability_tag = "PROBABILITY"

            filtered_setups.append([
                stock_clean, probability_tag, trigger_high, round(last_close, 2),
                demand_zone_low, next_gann_target, int(avg_vol), dry_ratio, now.strftime('%d-%b-%Y')
            ])

    except Exception:
        continue

# Update Sheet
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()
ws_ready.append_row([
    "Stock", "Probability_Tag", "Trigger_High", "Last_Close", 
    "Demand_Zone_Low", "Gann_Target", "Vol_SMA20", "Dry_Ratio", "Scan_Date"
])

if filtered_setups:
    filtered_setups.sort(key=lambda x: ("GANN" in x[1], "HIGH" in x[1]), reverse=True)
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 SUCCESS! Saved {len(filtered_setups)} stocks in 'Ready_For_Today'!")
else:
    ws_ready.append_row(["NO MATCHING SETUPS TODAY", "-", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
    
