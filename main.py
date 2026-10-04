from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== [VPA RBS FINAL] RUNNING LH RETEST + DRY VOL SCANNER | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(val) for val in row] for row in rows]

# 1. Connect to Google Sheets
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

# 2. Read Watchlist
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
print(f"📥 Scanning RBS Data for {len(clean_stocks)} stocks...", flush=True)

filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")

        df = yf.Ticker(symbol).history(period="70d", interval="1d")
        if df.empty or len(df) < 45:
            continue

        df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
        df['Spread'] = df['High'] - df['Low']
        df['Spread_SMA20'] = df['Spread'].rolling(window=20).mean()

        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])

        avg_vol = float(df['Vol_SMA20'].iloc[-1])
        avg_spread = float(df['Spread_SMA20'].iloc[-1])

        if math.isnan(avg_vol) or avg_vol < 30000 or math.isnan(last_close) or avg_spread <= 0:
            continue

        vol_ratio = last_vol / avg_vol
        last_spread = last_high - last_low
        spread_ratio = last_spread / avg_spread

        # -----------------------------------------------------------------
        # STEP 1: DOWNTREND + VOLUME DRY IN FALL
        # -----------------------------------------------------------------
        past_window = df.iloc[-35:-6] # 35 se 6 din pehle tak

        first_half_high = past_window['High'].iloc[:15].max()
        second_half_high = past_window['High'].iloc[15:].max()
        is_in_downtrend = second_half_high < first_half_high

        # Girte time volume average se kam hona chahiye - Bechne wala kamzor
        falling_vol_avg = past_window['Volume'].mean()
        is_falling_vol_dry = falling_vol_avg < (avg_vol * 1.0)

        # -----------------------------------------------------------------
        # STEP 2: VOLUME BLAST + LH BREAKOUT (Last 5 days)
        # -----------------------------------------------------------------
        recent_window = df.iloc[-6:-1] # Kal tak ke 5 din
        nearest_lh = float(past_window['High'].iloc[-15:].max())
        lh_date = past_window['High'].iloc[-15:].idxmax()
        days_since_lh = len(df) - df.index.get_loc(lh_date)

        # LH bahut purana nahi hona chahiye
        if days_since_lh > 25:
            continue

        has_volume_blast = False
        blast_vol_ratio = 0
        blast_close = 0

        for i in range(len(recent_window)):
            row = recent_window.iloc[i]
            c_vol_sma = float(row['Vol_SMA20'])
            if math.isnan(c_vol_sma) or c_vol_sma == 0:
                continue
            c_vol_ratio = float(row['Volume']) / c_vol_sma

            if c_vol_ratio >= 1.6 and float(row['High']) >= (nearest_lh * 0.998) and float(row['Close']) > float(row['Open']):
                has_volume_blast = True
                blast_vol_ratio = c_vol_ratio
                blast_close = float(row['Close'])
                break

        # -----------------------------------------------------------------
        # STEP 3: DRY VOLUME RETEST ON LH (Aaj ka din)
        # -----------------------------------------------------------------
        is_valid_retest = False

        if has_volume_blast:
            # 1. Price LH Support ke paas aaya?
            is_touching_support = (last_low <= nearest_lh * 1.02) and (last_close >= nearest_lh * 0.97) and (last_close <= blast_close * 1.03)

            # 2. VOLUME DRY-UP FILTER - Tera Sawal
            # Blast aur Aaj ke beech wale din + Aaj ka volume low hona chahiye
            vol_last_3_days = df['Volume'].iloc[-4:-1].mean() # Blast ke baad se ab tak
            is_vol_dry_before = vol_last_3_days < (avg_vol * 0.95)
            is_low_vol_today = vol_ratio <= 1.1 # Aaj retest pe volume high nahi hona chahiye

            # 3. Rejection Wick
            has_rejection_wick = (last_close - last_low) >= (0.35 * last_spread)
            is_green_or_doji = last_close >= last_open * 0.998

            is_valid_retest = is_touching_support and is_vol_dry_before and is_low_vol_today and has_rejection_wick and is_green_or_doji

        # -----------------------------------------------------------------
        # FINAL SAVE
        # -----------------------------------------------------------------
        if is_in_downtrend and is_falling_vol_dry and has_volume_blast and is_valid_retest:
            trigger_high = round(nearest_lh, 2)
            stop_loss = round(float(df['Low'].iloc[-4:].min() * 0.98), 2)
            vol_status = f"Blast:{round(blast_vol_ratio,1)}x -> Retest:{round(vol_ratio,1)}x Dry"

            probability_tag = "RBS LH RETEST + DRY VOL"

            filtered_setups.append([
                stock_clean, probability_tag, trigger_high, round(last_close, 2),
                stop_loss, vol_status, int(avg_vol), now.strftime('%d-%b-%Y')
            ])

    except Exception:
        continue

# 3. Update Google Sheet
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()
ws_ready.append_row([
    "Stock", "Probability_Tag", "LH_Support", "Last_Close",
    "StopLoss", "Volume_Status", "Vol_SMA20", "Scan_Date"
])

if filtered_setups:
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 SUCCESS! Saved {len(filtered_setups)} RBS setups in 'Ready_For_Today'!")
else:
    ws_ready.append_row(["NO RBS SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
    print("ℹ️ No stocks matched RBS + Dry Vol criteria today.")
