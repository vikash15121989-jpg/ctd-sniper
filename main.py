from datetime import datetime, timezone, timedelta
import json
import os
import gspread
import pandas as pd
import yfinance as yf

# =====================================================================
# 1. IST TIMEZONE SETUP
# =====================================================================
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== CTD SNIPER STARTING | Time: {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
        return sh.add_worksheet(title=title, rows="200", cols="10")

# =====================================================================
# POSITION SIZING CALCULATOR
# =====================================================================
def update_position_sizing_calculator():
    ws_pos = get_or_create_worksheet("Position_Sizing")
    
    existing_data = ws_pos.get_all_values()
    capital = 100000
    risk_pct = 1.0
    stock_input = "RELIANCE"

    if len(existing_data) >= 3:
        try:
            capital = float(existing_data[0][1].replace("₹", "").replace(",", "").strip())
        except: pass
        try:
            risk_pct = float(existing_data[1][1].replace("%", "").strip())
        except: pass
        try:
            stock_input = str(existing_data[2][1]).strip().upper()
        except: pass

    layout = [
        ["Total Capital (₹)", capital],
        ["Risk Per Trade (%)", risk_pct],
        ["Stock Name", stock_input],
        ["", ""],
        ["Stock Symbol", "CMP / Entry (₹)", "Stop Loss (20 EMA) (₹)", "Risk / Share (₹)", "Max Risk Amt (₹)", "Calculated Qty", "Total Investment (₹)", "Updated At"]
    ]

    calc_row = ["-", "-", "-", "-", "-", "-", "-", "-"]
    if stock_input and stock_input not in ["-", "NONE", ""]:
        try:
            symbol = stock_input + ".NS" if not stock_input.endswith(".NS") else stock_input
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="60d", interval="1d")
            
            if not df.empty and len(df) >= 20:
                df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
                cmp = round(float(df['Close'].iloc[-1]), 2)
                sl = round(float(df['EMA20'].iloc[-1]), 2)
                
                if sl >= cmp:
                    sl = round(cmp * 0.98, 2)

                risk_per_share = round(cmp - sl, 2)
                max_risk_amt = round((capital * risk_pct) / 100.0, 2)
                qty = int(max_risk_amt / risk_per_share) if risk_per_share > 0 else 0
                total_investment = round(qty * cmp, 2)

                calc_row = [
                    stock_input.replace(".NS", ""),
                    cmp,
                    sl,
                    risk_per_share,
                    max_risk_amt,
                    qty,
                    total_investment,
                    now.strftime('%H:%M IST')
                ]
        except Exception as e:
            print(f"⚠️ Error fetching Position Sizing data for {stock_input}: {e}")

    layout.append(calc_row)
    ws_pos.clear()
    ws_pos.update("A1", layout)
    print("📐 'Position_Sizing' Calculator updated successfully.")

update_position_sizing_calculator()

# =====================================================================
# TIME WINDOW CHECK (MARKET LIVE VS MARKET CLOSED)
# =====================================================================
market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

# Check if current time is within 09:15 AM to 03:30 PM IST
is_live_market = market_open_time <= now <= market_close_time

# =====================================================================
# STEP 1: INTRADAY LIVE SCAN (09:15 AM TO 03:30 PM IST)
# =====================================================================
if is_live_market:
    print("⚡ Running INTRADAY LIVE SCAN...", flush=True)
    
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"])

    if not records:
        print("⚠️ 'Ready_For_Today' sheet is empty.")
        ws_live.append_row(["NO TARGETS SET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        exit(0)

    df_ready = pd.DataFrame(records)

    # Minutes passed calculation (Minimum 5 minutes limit)
    mins_passed = max(int((now - market_open_time).total_seconds() / 60), 5)
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

            open_price = round(float(df_live['Open'].iloc[0]), 2)  
            price = round(float(df_live['Close'].iloc[-1]), 2)       
            vol = float(df_live['Volume'].sum())
            vol_ratio = round((vol * projected_factor) / v_sma20, 2) if v_sma20 > 0 else 0.0

            # 🚀 Breakout Condition: Price > Trigger High AND Projected Volume >= 1.8x AND Green Candle
            if price >= trigger and vol_ratio >= 1.8 and price > open_price:
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
        print(f"🚀 Found {len(confirmed_breakouts)} live green-candle breakouts!")
    else:
        ws_live.append_row(["NO BREAKOUT YET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        print("ℹ️ No volume surge breakouts matched at this time.")

# =====================================================================
# STEP 2: EOD SCAN (Market Closed: Before 09:15 AM or After 03:30 PM IST)
# =====================================================================
else:
    print("📌 Running EOD Scan: Filtering Volume Dry Setups...", flush=True)

    ws_dry_all = get_or_create_worksheet("Volume_Dry_All")
    ws_dry_all.clear()
    ws_dry_all.append_row(["Stock", "Trigger_High", "Last_Close", "Vol_SMA20", "Dry_Ratio_%", "Status"])

    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row(["Stock", "Trigger_High", "Last_Close", "Vol_SMA20", "Dry_Ratio_%", "EMA20"])

    try:
        raw_stocks = sh.worksheet("Watchlist").col_values(1)
    except Exception as e:
        print(f"❌ Error reading Watchlist: {e}")
        exit(1)

    STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
    
    all_dry_targets = []
    ready_targets = []

    for symbol in set(STOCKS):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="60d", interval="1d")
            if df.empty or len(df) < 30: 
                continue

            # 💡 Turnover Check (Min ₹3 Cr Turnover)
            avg_vol = float(df['Volume'].rolling(window=20).mean().iloc[-1])
            last_close = float(df['Close'].iloc[-1])
            daily_turnover_cr = (avg_vol * last_close) / 10000000.0

            if daily_turnover_cr < 3.0 or avg_vol < 200000:
                continue

            df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
            df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

            recent = df.iloc[-5:].copy()
            ema20 = float(recent['EMA20'].iloc[-1])
            ema50 = float(recent['EMA50'].iloc[-1])
            last_vol = float(recent['Volume'].iloc[-1])
            dry_ratio = round((last_vol / avg_vol) * 100, 1) if avg_vol > 0 else 100.0
            trigger_high = round(float(recent['High'].iloc[-1]), 2)
            stock_clean = symbol.replace(".NS", "")

            # Volume Dry Condition
            is_volume_dry = (recent['Volume'].iloc[-3:] < (0.50 * recent['Vol_SMA20'].iloc[-3:])).sum() >= 2

            if is_volume_dry:
                all_dry_targets.append([
                    stock_clean, trigger_high, round(last_close, 2), int(avg_vol), f"{dry_ratio}%", "DRY"
                ])

                in_uptrend = last_close >= (ema20 * 0.98) and last_close >= (ema50 * 0.98)
                is_consolidating = (recent['High'].max() / recent['Low'].min()) <= 1.10

                if in_uptrend and is_consolidating:
                    ready_targets.append([
                        stock_clean, trigger_high, round(last_close, 2), int(avg_vol), f"{dry_ratio}%", round(ema20, 2)
                    ])

        except Exception:
            continue

    if all_dry_targets:
        ws_dry_all.append_rows(all_dry_targets)
        print(f"✅ Saved ALL {len(all_dry_targets)} Volume Dry stocks to 'Volume_Dry_All'.")

    if ready_targets:
        ws_ready.append_rows(ready_targets)
        print(f"✅ Saved {len(ready_targets)} Quality stocks to 'Ready_For_Today'.")
        
