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

print(f"=== CTD SNIPER STARTING | Time: {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

# Helper functions to sanitize non-JSON float values (NaN, Inf)
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

# ETF Exclusion Keywords List
ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]

def is_etf(symbol_name):
    return any(kw in symbol_name.upper() for kw in ETF_KEYWORDS)

# =====================================================================
# TIME WINDOW CHECK (MARKET LIVE VS MARKET CLOSED)
# =====================================================================
market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

is_weekday = now.weekday() < 5
is_live_market = is_weekday and (market_open_time <= now <= market_close_time)

# =====================================================================
# ROUTING: MARKET HOURS vs OFF-MARKET
# =====================================================================

if is_live_market:
    # -----------------------------------------------------------------
    # STEP 1: INTRADAY LIVE SCAN (09:15 AM - 03:30 PM IST)
    # Ready_For_Today sheet ko READ karega, clear bilkul NAHI karega
    # -----------------------------------------------------------------
    print("⚡ [LIVE MARKET HOURS] Running Intraday Breakout Monitor...", flush=True)
    
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    records = ws_ready.get_all_records()

    ws_live = get_or_create_worksheet("LIVE_BREAKOUTS")
    ws_live.clear()
    ws_live.append_row(["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Tag", "Scan_Time"])

    if not records or "Stock" not in records[0]:
        print("⚠️ 'Ready_For_Today' sheet is empty or invalid.")
        ws_live.append_row(["NO TARGETS SET", "-", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        exit(0)

    df_ready = pd.DataFrame(records)

    mins_passed = max(int((now - market_open_time).total_seconds() / 60), 5)
    projected_factor = 375 / mins_passed

    confirmed_breakouts = []

    for _, row in df_ready.iterrows():
        try:
            stock_name = str(row['Stock']).strip().upper()
            if is_etf(stock_name): continue

            symbol = stock_name + ".NS"
            trigger = float(row['Trigger_High'])
            v_sma20 = float(row['Vol_SMA20']) if 'Vol_SMA20' in row else 100000.0
            tag = row['Probability_Tag'] if 'Probability_Tag' in row else "PROBABILITY"

            df_live = yf.Ticker(symbol).history(period="1d", interval="5m")
            if df_live.empty: continue

            open_price = round(float(df_live['Open'].iloc[0]), 2)  
            price = round(float(df_live['Close'].iloc[-1]), 2)       
            vol = float(df_live['Volume'].sum())
            vol_ratio = round((vol * projected_factor) / v_sma20, 2) if v_sma20 > 0 else 0.0

            if price >= trigger and vol_ratio >= 1.8 and price > open_price:
                confirmed_breakouts.append([
                    row['Stock'], price, trigger,
                    round(((price - trigger) / trigger) * 100, 2),
                    f"{vol_ratio}x",
                    tag,
                    now.strftime('%H:%M IST')
                ])
        except Exception:
            continue

    if confirmed_breakouts:
        ws_live.append_rows(sanitize_rows(confirmed_breakouts))
        print(f"🚀 Found {len(confirmed_breakouts)} live breakouts!")
    else:
        ws_live.append_row(["NO BREAKOUT YET", "-", "-", "-", "-", "-", now.strftime('%H:%M IST')])
        print("ℹ️ No live breakouts triggered yet.")

else:
    # -----------------------------------------------------------------
    # STEP 2: EOD SCAN (OFF-MARKET)
    # Strictly filters Volume Dry Setups (~300 Quality Stocks)
    # -----------------------------------------------------------------
    print("📌 [MARKET CLOSED] Running Full EOD Scanner (Volume Dry Filter)...", flush=True)

    try:
        raw_stocks = sh.worksheet("Watchlist").col_values(1)
    except Exception as e:
        print(f"❌ Error reading Watchlist: {e}")
        exit(1)

    STOCKS = [s.strip().upper() + ".NS" for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]
    clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))
    
    print(f"📥 Fetching EOD data for {len(clean_stocks)} stocks...", flush=True)
    
    filtered_setups = []

    # Batch Fetch with clean structure extraction
    batch_data = yf.download(clean_stocks, period="60d", interval="1d", group_by="ticker", threads=True, progress=False)

    for symbol in clean_stocks:
        try:
            stock_clean = symbol.replace(".NS", "")

            # Single ticker vs Multi-ticker MultiIndex Handling
            if len(clean_stocks) == 1:
                df = batch_data.copy()
            else:
                if symbol not in batch_data.columns.levels[0]: 
                    continue
                df = batch_data[symbol].dropna(how="all").copy()

            if df.empty or len(df) < 30: 
                continue

            # Indicators Calculation
            df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
            df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

            last_close = float(df['Close'].iloc[-1])
            avg_vol = float(df['Vol_SMA20'].iloc[-1])

            # Liquidity Filters: Min Avg Volume & Turnover > 3 Crore
            if math.isnan(avg_vol) or avg_vol < 200000 or math.isnan(last_close): 
                continue
            if ((avg_vol * last_close) / 10000000.0) < 3.0: 
                continue  

            recent_5d = df.iloc[-5:]
            recent_20d = df.iloc[-20:]
            
            last_vol = float(recent_5d['Volume'].iloc[-1])
            dry_ratio = f"{round((last_vol / avg_vol) * 100, 1)}%"
            trigger_high = round(float(recent_20d['High'].max()), 2)
            demand_zone_low = round(float(df['Low'].iloc[-30:].min()), 2)

            # 🎯 STRICT VOLUME DRY LOGIC:
            # Pichle 5 dino me kam se kam 2 din volume average ke 50% se KAM hona chahiye
            vol_check = (recent_5d['Volume'] < (0.50 * recent_5d['Vol_SMA20'])).sum()
            is_volume_dry = vol_check >= 2
            
            ema20 = float(recent_5d['EMA20'].iloc[-1])
            ema50 = float(recent_5d['EMA50'].iloc[-1])
            
            # Uptrend and Tight Range Check
            in_uptrend = last_close >= (ema20 * 0.98) and last_close >= (ema50 * 0.98)
            is_consolidating = (recent_5d['High'].max() / recent_5d['Low'].min()) <= 1.12

            if is_volume_dry and in_uptrend and is_consolidating:
                probability_tag = "HIGH PROBABILITY" if last_close >= ema20 else "PROBABILITY"
                filtered_setups.append([
                    stock_clean, probability_tag, trigger_high, round(last_close, 2),
                    demand_zone_low, int(avg_vol), dry_ratio, now.strftime('%d-%b-%Y')
                ])
        except Exception as e:
            continue

    # Update Google Sheet
    ws_ready = get_or_create_worksheet("Ready_For_Today")
    ws_ready.clear()
    ws_ready.append_row([
        "Stock", "Probability_Tag", "Trigger_High", "Last_Close", 
        "Demand_Zone_Low", "Vol_SMA20", "Dry_Ratio", "Scan_Date"
    ])

    filtered_setups.sort(key=lambda x: x[1], reverse=True)

    if filtered_setups:
        ws_ready.append_rows(sanitize_rows(filtered_setups))
        print(f"🎯 Saved {len(filtered_setups)} Strictly Filtered Volume Dry Stocks to 'Ready_For_Today'.")
    else:
        ws_ready.append_row(["NO MATCHING SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
        print("ℹ️ No equity stocks matched the setup criteria today.")
