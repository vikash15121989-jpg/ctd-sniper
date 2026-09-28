from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import yfinance as yf

# =====================================================================
# 1. IST TIMEZONE SETUP
# =====================================================================
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== EOD RESISTANCE & GANN SCANNER | Time: {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(val) for val in row] for row in rows]

# =====================================================================
# GANN SQUARE OF 9 HELPER FUNCTIONS
# =====================================================================
def calculate_gann_levels(price):
    """Calculates Gann Square of 9 levels and next target based on price."""
    if price <= 0:
        return [], 0.0
    
    root = math.sqrt(price)
    
    # Resistance Angles: +90 deg (+0.5), +180 deg (+1.0), +270 deg (+1.5), +360 deg (+2.0)
    gann_resistances = [round((root + f)**2, 2) for f in [0.5, 1.0, 1.5, 2.0]]
    
    # Next major 360-degree target (+2.0 factor)
    next_360_target = round((root + 2.0)**2, 2)
    
    return gann_resistances, next_360_target

# =====================================================================
# 2. CONNECT TO GOOGLE SHEETS
# =====================================================================
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

# =====================================================================
# 3. EOD SCANNER LOGIC (25 SEPTEMBER FOCUS)
# =====================================================================
try:
    raw_stocks = sh.worksheet("Watchlist").col_values(1)
except Exception as e:
    print(f"❌ Error reading Watchlist: {e}")
    exit(1)

STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))

print(f"📥 Scanning EOD data up to 25-Sep for {len(clean_stocks)} stocks...", flush=True)

filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        
        # Fetch daily history
        df = yf.Ticker(symbol).history(period="60d", interval="1d")
        if df.empty:
            continue

        # Filter strictly up to 25 September 2026 EOD
        df_until_25 = df.loc[:"2026-09-25"]
        if len(df_until_25) < 35:
            continue

        df_until_25['Vol_SMA20'] = df_until_25['Volume'].rolling(window=20).mean()

        last_close = float(df_until_25['Close'].iloc[-1])
        avg_vol = float(df_until_25['Vol_SMA20'].iloc[-1])

        # Liquidity Filter
        if math.isnan(avg_vol) or avg_vol < 50000 or math.isnan(last_close):
            continue  

        df_40d = df_until_25.iloc[-40:]
        recent_3d = df_until_25.iloc[-3:]  # Pullback days window

        # 1. Recent Resistance (Peak 1) & Breakout Day Index
        recent_resistance = float(df_40d['High'].max())
        peak1_idx = df_40d['High'].idxmax()

        # 2. Previous Resistance (Peak 2)
        df_prior = df_40d.drop(peak1_idx)
        prior_resistance = float(df_prior['High'].max())

        # Rule 1: Recent Resistance >= 98% of Previous Resistance
        is_cross_or_near = recent_resistance >= (prior_resistance * 0.98)

        # Rule 2: Breakout Day Volume >= 1.5x Vol_SMA20
        breakout_day_vol = float(df_40d.loc[peak1_idx, 'Volume'])
        breakout_day_avg_vol = float(df_40d.loc[peak1_idx, 'Vol_SMA20'])
        is_breakout_day_high_vol = (breakout_day_vol >= 1.5 * breakout_day_avg_vol)

        # Rule 3: Low Volume Pullback in Recent 3 Days
        last_vol = float(recent_3d['Volume'].iloc[-1])
        is_low_vol_pullback = (recent_3d['Volume'] < recent_3d['Vol_SMA20']).sum() >= 1

        # Gann Confluence Analysis
        gann_resistances, next_gann_target = calculate_gann_levels(recent_resistance)
        is_gann_confluence = any(abs(recent_resistance - g_res) / g_res <= 0.015 for g_res in gann_resistances)

        # Final Filter
        if is_cross_or_near and is_breakout_day_high_vol and is_low_vol_pullback:
            trigger_high = round(recent_resistance, 2)
            demand_zone_low = round(float(df_until_25['Low'].iloc[-20:].min()), 2)
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
                demand_zone_low, next_gann_target, int(avg_vol), dry_ratio, "25-Sep-2026"
            ])

    except Exception:
        continue

# Output to Google Sheets
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()
ws_ready.append_row([
    "Stock", "Probability_Tag", "Trigger_High", "25Sep_Close", 
    "Demand_Zone_Low", "Gann_Target", "Vol_SMA20", "Dry_Ratio", "Scan_Date"
])

if filtered_setups:
    filtered_setups.sort(key=lambda x: ("GANN" in x[1], "HIGH" in x[1]), reverse=True)
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 Saved {len(filtered_setups)} stocks in 'Ready_For_Today' based on 25-Sep EOD Data!")
else:
    ws_ready.append_row(["NO MATCHING SETUPS ON 25-SEP", "-", "-", "-", "-", "-", "-", "-", "25-Sep-2026"])
    
