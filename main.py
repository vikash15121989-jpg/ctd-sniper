from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import yfinance as yf

# =====================================================================
# 1. IST TIMEZONE SETUP & TIME WINDOW CHECK
# =====================================================================
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== CTD SNIPER RUNNER | Time: {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

is_weekday = now.weekday() < 5
is_live_market = is_weekday and (market_open_time <= now <= market_close_time)

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
    if price <= 0:
        return [], 0.0
    
    root = math.sqrt(price)
    gann_resistances = [round((root + f)**2, 2) for f in [0.5, 1.0, 1.5, 2.0]]
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
# 3. ROUTING LOGIC: EOD SCAN vs LIVE SCAN
# =====================================================================

if not is_live_market:
    # -----------------------------------------------------------------
    # OFF-MARKET HOURS: EOD SCANNER (UPDATES 'Ready_For_Today')
    # -----------------------------------------------------------------
    print("🌙 [OFF-MARKET RUN] Executing EOD Resistance Breakout + Gann Confluence Scanner...", flush=True)

    try:
        raw_stocks = sh.worksheet("Watchlist").col_values(1)
    except Exception as e:
        print(f"❌ Error reading Watchlist: {e}")
        exit(1)

    STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
    clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))
    
    print(f"📥 Fetching EOD data for {len(clean_stocks)} stocks from Watchlist...", flush=True)
    
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

            if math.isnan(avg_vol) or avg_vol < 50000 or math.isnan(last_close): 
                continue  

            df_40d = df.iloc[-40:]
            recent_3d = df.iloc[-3:]

            recent_resistance = float(df_40d['High'].max())
            peak1_idx = df_40d['High'].idxmax()

            df_prior = df_40d.drop(peak1_idx)
            prior_resistance = float(df_prior['High'].max())

            # Rule 1: Peak 1 vs Peak 2
            is_cross_or_near = recent_resistance >= (prior_resistance * 0.98)

            # Rule 2: Peak 1 Day High Volume
            breakout_day_vol = float(df_40d.loc[peak1_idx, 'Volume'])
            breakout_day_avg_vol = float(df_40d.loc[peak1_idx, 'Vol_SMA20'])
            is_breakout_day_high_vol = (breakout_day_vol >= 1.5 * breakout_day_avg_vol)

            # Rule 3: Recent Low Volume Pullback
            last_vol = float(recent_3d['Volume'].iloc[-1])
            is_low_vol_pullback = (recent_3d['Volume'] < recent_3d['Vol_SMA20']).sum() >= 1

            # Rule 4: Proximity Filter (Last close must be within 3% of Resistance)
            is_near_resistance = (last_close >= recent_resistance * 0.97) and (last_close <= recent_resistance * 1.03)

            # Rule 5: Gann Confluence
            gann_resistances, next_gann_target = calculate_gann_levels(recent_resistance)
            is_gann_confluence = any(abs(recent_resistance - g_res) / g_res <= 0.015 for g_res in gann_resistances)

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

    # Update 'Ready_For_Today' Sheet Only During Off-Market Hours
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row([
        "Stock", "Probability_Tag", "Trigger_High", "Last_Close", 
        "Demand_Zone_Low", "Gann_Target", "Vol_SMA20", "Dry_Ratio", "Scan_Date"
    ])

    if filtered_setups:
        filtered_setups.sort(key=lambda x: ("GANN" in x[1], "HIGH" in x[1]), reverse=True)
        ws_ready.append_rows(sanitize_rows(filtered_setups))
        print(f"🎯 SAVED {len(filtered_setups)} valid stocks in 'Ready_For_Today'!")
    else:
        ws_ready.append_row(["NO MATCHING SETUPS TODAY", "-", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])

else:
    # -----------------------------------------------------------------
    # LIVE MARKET HOURS: READ-ONLY 'Ready_For_Today' (DO NOT TOUCH IT)
    # -----------------------------------------------------------------
    print("⚡ [LIVE MARKET HOURS] Reading target list from 'Ready_For_Today'...", flush=True)

    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gann_Target", "15m_Candle_Vol_Spike", "Probability_Tag", "Entry_Time"])

    if not records or "Stock" not in records[0] or str(records[0]['Stock']).startswith("NO MATCHING"):
        print("⚠️ No pre-filtered targets found in 'Ready_For_Today'.")
        ws_live.append_row(["NO TARGETS SET IN READY_FOR_TODAY", "-", "-", "-", "-", "-", now.strftime('%H:%M IST')])
    else:
        df_ready = pd.DataFrame(records)
        confirmed_breakouts = []

        for _, row in df_ready.iterrows():
            try:
                stock_name = str(row['Stock']).strip().upper()
                if is_etf(stock_name) or not stock_name: 
                    continue

                symbol = stock_name + ".NS"
                trigger = float(row['Trigger_High'])
                gann_target = row.get('Gann_Target', '-')
                tag = row.get('Probability_Tag', 'PROBABILITY')

                df_live_15m = yf.Ticker(symbol).history(period="1d", interval="15m")
                if df_live_15m.empty or len(df_live_15m) < 2: 
                    continue

                latest_candle = df_live_15m.iloc[-1]
                prev_candles = df_live_15m.iloc[:-1]

                live_price = round(float(latest_candle['Close']), 2)
                candle_vol = float(latest_candle['Volume'])
                avg_15m_vol = float(prev_candles['Volume'].mean()) if len(prev_candles) > 0 else 1.0

                vol_ratio = round(candle_vol / avg_15m_vol, 2) if avg_15m_vol > 0 else 0.0

                # Live Breakout Filter (Live Price >= Trigger & 15m Vol Spike >= 2.0x & Green Candle)
                if live_price >= (trigger * 0.995) and vol_ratio >= 2.0 and live_price >= float(latest_candle['Open']):
                    confirmed_breakouts.append([
                        stock_name, 
                        live_price, 
                        trigger, 
                        gann_target,
                        f"{vol_ratio}x Spike", 
                        tag, 
                        now.strftime('%H:%M IST')
                    ])
            except Exception:
                continue

        if confirmed_breakouts:
            ws_live.append_rows(sanitize_rows(confirmed_breakouts))
            print(f"🚀 VOLUME BLAST DETECTED for {len(confirmed_breakouts)} stocks!")
        else:
            ws_live.append_row(["NO VOLUME BLAST DETECTED YET", "-", "-", "-", "-", "-", now.strftime('%H:%M IST')])
            print("ℹ️ Pre-selected stocks are quiet right now.")
            
