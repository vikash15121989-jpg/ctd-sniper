from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import yfinance as yf

# =====================================================================
# 📌 FORCE EOD FLAG
# Live Market hours me bhi EOD Scan chala kar Sheet bharne ke liye TRUE karein.
# EOD Scan run karne ke baad ise FALSE kar dena.
# =====================================================================
FORCE_EOD_RUN = False 

# =====================================================================
# 1. IST TIMEZONE SETUP
# =====================================================================
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== CTD SNIPER STARTING | Time: {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(val) for val in row] for row in rows]

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

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]

def is_etf(symbol_name):
    return any(kw in symbol_name.upper() for kw in ETF_KEYWORDS)

# =====================================================================
# TIME WINDOW CHECK
# =====================================================================
market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

is_weekday = now.weekday() < 5
is_live_market = is_weekday and (market_open_time <= now <= market_close_time)

if FORCE_EOD_RUN:
    print("⚠️ FORCE_EOD_RUN is TRUE: Bypassing Live Market check to rebuild 'Ready_For_Today' sheet!", flush=True)
    is_live_market = False

# =====================================================================
# ROUTING: LIVE MARKET HOURS vs EOD SCAN (OFF-MARKET)
# =====================================================================

if is_live_market:
    # -----------------------------------------------------------------
    # INTRADAY MONITOR: ALL SHORTLISTED STOCKS FOR 15-MIN VOLUME BLAST
    # -----------------------------------------------------------------
    print("⚡ [LIVE MARKET HOURS] Scanning ALL shortlisted stocks for 15m Volume Blast...", flush=True)
    
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "15m_Candle_Vol_Spike", "Probability_Tag", "Entry_Time"])

    if not records or "Stock" not in records[0]:
        print("⚠️ 'Ready_For_Today' sheet is empty or invalid.")
        ws_live.append_row(["NO TARGETS SET IN READY_FOR_TODAY", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        exit(0)

    df_ready = pd.DataFrame(records)
    confirmed_breakouts = []

    for _, row in df_ready.iterrows():
        try:
            stock_name = str(row['Stock']).strip().upper()
            if is_etf(stock_name): continue

            symbol = stock_name + ".NS"
            trigger = float(row['Trigger_High'])
            tag = row['Probability_Tag'] if 'Probability_Tag' in row else "PROBABILITY"

            # 15-minute timeframe data fetch
            df_live_15m = yf.Ticker(symbol).history(period="1d", interval="15m")
            if df_live_15m.empty or len(df_live_15m) < 2: continue

            latest_candle = df_live_15m.iloc[-1]
            prev_candles = df_live_15m.iloc[:-1]

            live_price = round(float(latest_candle['Close']), 2)
            candle_vol = float(latest_candle['Volume'])
            avg_15m_vol = float(prev_candles['Volume'].mean()) if len(prev_candles) > 0 else 1.0

            vol_ratio = round(candle_vol / avg_15m_vol, 2) if avg_15m_vol > 0 else 0.0

            # 🎯 ENTRY TRIGGER CONDITIONS:
            # 1. Price Trigger High ke paas ya uske upar ho (Within 0.5% or Breakout)
            # 2. 15-Minute candle me Average 15m volume se 2.5x ya usse bada Volume Blast ho
            # 3. Green Candle Close Direction (Close >= Open)
            if live_price >= (trigger * 0.995) and vol_ratio >= 2.5 and live_price >= float(latest_candle['Open']):
                confirmed_breakouts.append([
                    stock_name, 
                    live_price, 
                    trigger, 
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
        ws_live.append_row(["NO VOLUME BLAST YET", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        print("ℹ️ Scanning complete. Waiting for intraday volume blast...")

else:
    # -----------------------------------------------------------------
    # EOD SCANNER: DYNAMIC RESISTANCE + LOW VOLUME PULLBACK
    # -----------------------------------------------------------------
    print("📌 Running Dynamic Resistance EOD Scanner...", flush=True)

    try:
        raw_stocks = sh.worksheet("Watchlist").col_values(1)
    except Exception as e:
        print(f"❌ Error reading Watchlist: {e}")
        exit(1)

    STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
    clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))
    
    print(f"📥 Fetching EOD data for {len(clean_stocks)} stocks...", flush=True)
    
    filtered_setups = []
    batch_data = yf.download(clean_stocks, period="60d", interval="1d", group_by="ticker", threads=True, progress=False)

    for symbol in clean_stocks:
        try:
            stock_clean = symbol.replace(".NS", "")

            if len(clean_stocks) == 1:
                df = batch_data.copy()
            else:
                if symbol not in batch_data.columns.levels[0]: 
                    continue
                df = batch_data[symbol].dropna(how="all").copy()

            if df.empty or len(df) < 40: 
                continue

            df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()

            last_close = float(df['Close'].iloc[-1])
            avg_vol = float(df['Vol_SMA20'].iloc[-1])

            # Liquidity Filter (Avg Vol >= 1.5 Lakh & Turnover >= 2.5 Cr)
            if math.isnan(avg_vol) or avg_vol < 150000 or math.isnan(last_close): 
                continue
            if ((avg_vol * last_close) / 10000000.0) < 2.5: 
                continue  

            # -----------------------------------------------------------------
            # DYNAMIC RESISTANCE STRUCTURE (Pichle 40-Days Lookback Window)
            # -----------------------------------------------------------------
            df_40d = df.iloc[-40:]
            recent_5d = df.iloc[-5:]

            max_high_40d = float(df_40d['High'].max())
            peak1_idx = df_40d['High'].idxmax()
            
            df_without_peak1 = df_40d.drop(peak1_idx)
            max_high_other = float(df_without_peak1['High'].max())

            recent_resistance = max_high_40d
            prior_resistance = max_high_other

            # RULE 1: Resistance Cross ya Zone Check (within 2%)
            has_crossed_or_in_zone = recent_resistance >= (prior_resistance * 0.98)

            # RULE 2: High Volume Breakout/Attempt (Vol >= 2.0x SMA20)
            vol_ratios_recent = df_40d['Volume'] / df_40d['Vol_SMA20']
            had_high_vol_breakout = (vol_ratios_recent >= 2.0).any()

            # RULE 3: Low Volume Pullback (Recent 5 days me at least 2 days Vol < 60% SMA)
            dry_days_count = (recent_5d['Volume'] < (0.60 * recent_5d['Vol_SMA20'])).sum()
            is_low_vol_pullback = dry_days_count >= 2

            if has_crossed_or_in_zone and had_high_vol_breakout and is_low_vol_pullback:
                trigger_high = round(recent_resistance, 2)
                demand_zone_low = round(float(df['Low'].iloc[-30:].min()), 2)
                ema20 = float(recent_5d['EMA20'].iloc[-1])
                last_vol = float(recent_5d['Volume'].iloc[-1])
                dry_ratio = f"{round((last_vol / avg_vol) * 100, 1)}%"

                # RULE 4: TAGGING
                # Agar Pullback Demand Zone / EMA20 Support ke paas (within 4%) ho -> HIGH PROBABILITY
                # Otherwise -> PROBABILITY
                is_near_demand = (last_close <= demand_zone_low * 1.04) or (abs(last_close - ema20) / ema20 <= 0.03)
                
                probability_tag = "HIGH PROBABILITY" if is_near_demand else "PROBABILITY"

                filtered_setups.append([
                    stock_clean, probability_tag, trigger_high, round(last_close, 2),
                    demand_zone_low, int(avg_vol), dry_ratio, now.strftime('%d-%b-%Y')
                ])

        except Exception:
            continue

    # Google Sheet Update
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row([
        "Stock", "Probability_Tag", "Trigger_High", "Last_Close", 
        "Demand_Zone_Low", "Vol_SMA20", "Dry_Ratio", "Scan_Date"
    ])

    filtered_setups.sort(key=lambda x: x[1], reverse=True)

    if filtered_setups:
        ws_ready.append_rows(sanitize_rows(filtered_setups))
        print(f"🎯 Saved {len(filtered_setups)} Filtered Setups to 'Ready_For_Today'.")
    else:
        ws_ready.append_row(["NO MATCHING SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
        print("ℹ️ No equity stocks matched the setup criteria today.")
        
