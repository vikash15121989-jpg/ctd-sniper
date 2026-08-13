from datetime import datetime, timezone, timedelta
import json
import os
import gspread
import pandas as pd
import yfinance as yf

# 1. IST Timezone Setup
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
current_hour = now.hour

print(f"=== CTD SNIPER STARTING | Time: {now.strftime('%H:%M IST')} ===", flush=True)

# 2. CONNECT TO GOOGLE SHEETS
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheet: CTD_Sniper", flush=True)
except Exception as e:
    print(f"❌ Error connecting to Google Sheets: {e}")
    exit(1)

# Helper function: Worksheet ko safely lene ya banane ke liye
def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="100", cols="10")

# =====================================================================
# STEP 1: EOD SCAN (Market Closed: >= 16:00 or < 09:00 IST)
# Filter 'Watchlist' -> Save to 'Ready_For_Today'
# =====================================================================
if current_hour >= 16 or current_hour < 9:
    print("📌 Running EOD Scan: Filtering Volume Dry-up Setups...", flush=True)

    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row(["Stock", "Trigger_High", "Last_Close", "Vol_SMA20", "Dry_Ratio"])

    try:
        raw_stocks = sh.worksheet("Watchlist").col_values(1)
    except Exception as e:
        print(f"❌ Error reading Watchlist: {e}")
        exit(1)

    STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
    
    ready_targets = []
    for symbol in set(STOCKS):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="60d", interval="1d")
            if df.empty or len(df) < 30: 
                continue

            df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
            recent = df.iloc[-5:].copy()
            
            # Logic: Volume Dry (Last 3 days volume < 55% of SMA)
            avg_vol = float(df['Vol_SMA20'].iloc[-1])
            is_dry = (recent['Volume'].iloc[-3:] < (0.55 * avg_vol)).sum() >= 2
            
            if is_dry:
                ready_targets.append([
                    symbol.replace(".NS", ""),
                    round(float(recent['High'].iloc[-1]), 2),
                    round(float(recent['Close'].iloc[-1]), 2),
                    int(avg_vol),
                    f"{round((recent['Volume'].iloc[-1] / avg_vol) * 100, 1)}%"
                ])
        except Exception:
            continue

    if ready_targets:
        ws_ready.append_rows(ready_targets)
        print(f"✅ Saved {len(ready_targets)} stocks to 'Ready_For_Today'.")
    else:
        print("ℹ️ No Volume Dry targets found today.")

# =====================================================================
# STEP 2: INTRADAY LIVE SCAN (Market Hours: 09:00 to 15:59 IST)
# Reads 'Ready_For_Today' -> Pushes Breakouts to 'LIVE_BREAKOUTS'
# =====================================================================
else:
    print("⚡ Running INTRADAY LIVE SCAN...", flush=True)
    
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    if not records:
        print("⚠️ 'Ready_For_Today' sheet is empty or tab was just created. Run EOD scan first!")
        ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
        ws_live.clear()
        ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"])
        ws_live.append_row(["NO TARGETS SET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        exit(0) # Smooth exit without error code

    df_ready = pd.DataFrame(records)
    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"])

    # Volume projection math
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    mins_passed = max(int((now - market_start).total_seconds() / 60), 5)
    projected_factor = 375 / mins_passed

    confirmed_breakouts = []

    for _, row in df_ready.iterrows():
        try:
            symbol = str(row['Stock']).strip() + ".NS"
            trigger = float(row['Trigger_High'])
            v_sma20 = float(row['Vol_SMA20'])

            df_live = yf.Ticker(symbol).history(period="1d", interval="5m")
            if df_live.empty: 
                continue

            price = round(float(df_live['Close'].iloc[-1]), 2)
            vol = float(df_live['Volume'].sum())
            vol_ratio = round((vol * projected_factor) / v_sma20, 2) if v_sma20 > 0 else 0.0

            # TRIGGER: Price >= Trigger High AND Volume Surge >= 1.8x
            if price >= trigger and vol_ratio >= 1.8:
                confirmed_breakouts.append([
                    row['Stock'], price, trigger,
                    round(((price - trigger) / trigger) * 100, 2),
                    f"{vol_ratio}x",
                    now.strftime('%H:%M IST')
                ])
        except Exception:
            continue

    if confirmed_breakouts:
        ws_live.append_rows(confirmed_breakouts)
        print(f"🚀 Found {len(confirmed_breakouts)} live breakouts!")
    else:
        ws_live.append_row(["NO BREAKOUT YET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        print("ℹ️ No volume surge breakouts matched at this time.")
        
