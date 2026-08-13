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

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="100", cols="10")

# =====================================================================
# STEP 1: EOD SCAN (STRICT HIGH-QUALITY FILTER: 16:00 to 09:00 IST)
# =====================================================================
if current_hour >= 16 or current_hour < 9:
    print("📌 Running STRICT EOD Scan for Quality Volume Dry Setups...", flush=True)

    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row(["Stock", "Trigger_High", "Last_Close", "Vol_SMA20", "Dry_Ratio", "EMA20"])

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
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
            df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

            recent = df.iloc[-5:].copy()
            avg_vol = float(df['Vol_SMA20'].iloc[-1])
            last_close = float(recent['Close'].iloc[-1])
            ema20 = float(recent['EMA20'].iloc[-1])
            ema50 = float(recent['EMA50'].iloc[-1])
            
            daily_turnover_cr = (avg_vol * last_close) / 10000000

            # 🛑 FILTER 1: High Liquidity Only (Min 5 Crore Turnover & 3 Lakh Volume)
            if daily_turnover_cr < 5.0 or avg_vol < 300000:
                continue

            # 🛑 FILTER 2: Uptrend/Support Filter (Price must be ABOVE or within 2% of 20 EMA & 50 EMA)
            if last_close < (ema20 * 0.98) or last_close < (ema50 * 0.98):
                continue

            # 🛑 FILTER 3: EXTREME VOLUME DRY-UP (At least 2 days vol < 40% of 20 SMA)
            extreme_dry_days = (recent['Volume'].iloc[-3:] < (0.40 * recent['Vol_SMA20'].iloc[-3:])).sum()
            if extreme_dry_days < 2:
                continue

            # 🛑 FILTER 4: Pullback/Consolidation (Not chasing multi-day runaway rally)
            price_consolidating = recent['High'].max() / recent['Low'].min() <= 1.08  # Max 8% range in 5 days

            if price_consolidating:
                trigger_high = round(float(recent['High'].iloc[-1]), 2)
                ready_targets.append({
                    "data": [
                        symbol.replace(".NS", ""),
                        trigger_high,
                        round(last_close, 2),
                        int(avg_vol),
                        f"{round((recent['Volume'].iloc[-1] / avg_vol) * 100, 1)}%",
                        round(ema20, 2)
                    ],
                    "dry_pct": float(recent['Volume'].iloc[-1] / avg_vol)
                })
        except Exception:
            continue

    # Sort by Most Volume Dry stocks first & pick Top 30 Best Setups
    if ready_targets:
        ready_targets.sort(key=lambda x: x['dry_pct'])
        final_rows = [item['data'] for item in ready_targets[:30]] # Max 30 Top Stocks
        
        ws_ready.append_rows(final_rows)
        print(f"✅ Filtered down to Top {len(final_rows)} Quality stocks in 'Ready_For_Today'!")
    else:
        print("ℹ️ No Quality Volume Dry targets found today.")

# =====================================================================
# STEP 2: INTRADAY LIVE SCAN (Market Hours: 09:00 to 15:59 IST)
# =====================================================================
else:
    print("⚡ Running INTRADAY LIVE SCAN...", flush=True)
    
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    if not records:
        print("⚠️ 'Ready_For_Today' sheet is empty. Run EOD scan first!")
        ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
        ws_live.clear()
        ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"])
        ws_live.append_row(["NO TARGETS SET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        exit(0)

    df_ready = pd.DataFrame(records)
    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"])

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

            open_price = round(float(df_live['Open'].iloc[0]), 2)  # Aaj ka Open Price
            price = round(float(df_live['Close'].iloc[-1]), 2)       # Live Price (CMP)
            vol = float(df_live['Volume'].sum())
            vol_ratio = round((vol * projected_factor) / v_sma20, 2) if v_sma20 > 0 else 0.0

            # 🟢 STRICT LIVE CONDITIONS:
            # 1. Price >= Trigger High (Breakout)
            # 2. Volume Surge >= 2.0x (Volume Expansion)
            # 3. CMP > Open Price (Green Day Candle - Buying Pressure only)
            if price >= trigger and vol_ratio >= 2.0 and price > open_price:
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
        
