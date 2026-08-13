from datetime import datetime
import json
import os
import gspread
import pandas as pd
import yfinance as yf

print("=== STARTING LIVE VOLUME SURGE SCANNER FOR GOOGLE SHEETS ===", flush=True)

# 1. CONNECT TO GOOGLE SHEETS
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    
    # Read Shortlisted Targets from EOD Scan
    ws_targets = sh.worksheet("Today_Targets")
    records = ws_targets.get_all_records()
    
    if not records:
        print("❌ No shortlisted stocks found in 'Today_Targets' tab.")
        exit(0)
        
    df_targets = pd.DataFrame(records)
    print(f"✅ Loaded {len(df_targets)} shortlisted target stocks from Google Sheet.", flush=True)
except Exception as e:
    print(f"❌ Error connecting to Google Sheets: {e}")
    exit(1)

# 2. PREPARE OR CREATE 'LIVE_BREAKOUTS' WORKSHEET
try:
    ws_live = sh.worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
except Exception:
    ws_live = sh.add_worksheet(title="LIVE_BREAKOUTS", rows="100", cols="10")

# 3. CALCULATE INTRADAY PROJECTED VOLUME SURGE
now = datetime.now()
market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
mins_passed = max(int((now - market_start).total_seconds() / 60), 5)

# Total market time = 375 mins
projected_factor = 375 / mins_passed

confirmed_breakouts = []

for _, row in df_targets.iterrows():
    symbol = str(row['Stock']).strip()
    if not symbol.endswith(".NS"):
        symbol += ".NS"
        
    m_high = float(row['Mother_High'])
    v_sma20 = float(row['Vol_SMA20'])

    try:
        # Download Today's 5-Min Intraday Data
        df_live = yf.download(symbol, period="1d", interval="5m", progress=False)
        if isinstance(df_live.columns, pd.MultiIndex):
            df_live.columns = df_live.columns.get_level_values(0)

        if df_live.empty:
            continue

        live_price = round(float(df_live['Close'].iloc[-1]), 2)
        current_vol = float(df_live['Volume'].sum())

        # Projected Day Volume
        projected_vol = current_vol * projected_factor
        vol_ratio = round(projected_vol / v_sma20, 2) if v_sma20 > 0 else 0.0

        # TRIGGER CONDITION:
        # Price Mother High ko cross/touch kar raha ho + Projected Volume >= 2.0x
        if live_price >= m_high and vol_ratio >= 2.0:
            confirmed_breakouts.append({
                "Stock": symbol.replace(".NS", ""),
                "Live_Price": live_price,
                "Mother_High": m_high,
                "Projected_Vol_Ratio": f"{vol_ratio}x",
                "Scan_Time": now.strftime('%H:%M IST')
            })
            print(f"🔥 BREAKOUT MATCH: {symbol} | Price: {live_price} | Vol Ratio: {vol_ratio}x")

    except Exception:
        pass

# 4. WRITE CONFIRMED BREAKOUTS DIRECTLY TO GOOGLE SHEET
if confirmed_breakouts:
    df_out = pd.DataFrame(confirmed_breakouts)
    ws_live.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"✅ Successfully wrote {len(confirmed_breakouts)} breakout stocks to 'LIVE_BREAKOUTS' sheet!")
else:
    # Header format agar koi stock match nahi hua
    ws_live.update([["Stock", "Live_Price", "Mother_High", "Projected_Vol_Ratio", "Scan_Time"], ["NO BREAKOUT YET", "-", "-", "-", now.strftime('%H:%M IST')]])
    print("ℹ️ No volume surge breakouts matched at this time.")
    
