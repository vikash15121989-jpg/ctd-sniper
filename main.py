from datetime import datetime
import json
import os
import gspread
import pandas as pd
import yfinance as yf

print("=== STARTING CTD SNIPER AUTOMATED SCANNER ===", flush=True)

# 1. CONNECT TO GOOGLE SHEETS
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheet: CTD_Sniper", flush=True)
except Exception as e:
    print(f"❌ Error connecting to Google Sheets: {e}")
    exit(1)

now = datetime.now()
current_hour = now.hour

# =====================================================================
# STEP 1: EOD SCAN - VOLUME DRY FILTER (Post 16:00 IST or Early Morning)
# Filter stocks from 'Watchlist' -> Save to 'Ready_For_Today'
# =====================================================================
if current_hour >= 16 or current_hour < 9:
    print("📌 Running EOD Scan: Filtering Volume Dry-up Setups...", flush=True)

    try:
        ws_ready = sh.worksheet("Ready_For_Today")
        ws_ready.clear()
    except Exception:
        ws_ready = sh.add_worksheet(title="Ready_For_Today", rows="100", cols="10")

    ws_watchlist = sh.worksheet("Watchlist")
    raw_stocks = ws_watchlist.col_values(1)

    STOCKS = []
    REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not any(k in clean_s for k in REJECT_KEYWORDS):
                if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                    clean_s += ".NS"
                STOCKS.append(clean_s)
    STOCKS = sorted(list(set(STOCKS)))

    ready_targets = []
    for symbol in STOCKS:
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="60d", interval="1d")

            if df.empty or len(df) < 30:
                continue

            df = df.reset_index()
            df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
            df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

            # Focus on last 5 completed daily candles
            recent = df.iloc[-5:].copy()

            avg_vol_20 = float(df['Vol_SMA20'].iloc[-1])
            last_close = float(recent['Close'].iloc[-1])
            ema50 = float(recent['EMA50'].iloc[-1])
            avg_daily_val = avg_vol_20 * last_close

            # Basic liquidity criteria
            if avg_vol_20 < 500000 or avg_daily_val < 20000000:
                continue

            # LOGIC 1: Price is pulling down or consolidating
            price_pullback = recent['Close'].iloc[-1] < recent['High'].iloc[-4]

            # LOGIC 2: VOLUME DRY-UP (Volume < 55% of 20-day Average for at least 2 of last 3 days)
            dry_days_count = (recent['Volume'].iloc[-3:] < (0.55 * recent['Vol_SMA20'].iloc[-3:])).sum()
            is_volume_dry = dry_days_count >= 2

            # LOGIC 3: Price above or holding near 50 EMA support
            near_support = last_close >= (ema50 * 0.97)

            if price_pullback and is_volume_dry and near_support:
                trigger_high = round(float(recent['High'].iloc[-1]), 2)  # Reversal trigger
                ready_targets.append({
                    "Stock": symbol.replace(".NS", ""),
                    "Trigger_High": trigger_high,
                    "Last_Close": round(last_close, 2),
                    "Vol_SMA20": int(avg_vol_20),
                    "Recent_Vol": int(recent['Volume'].iloc[-1]),
                    "Dry_Ratio": f"{round((recent['Volume'].iloc[-1] / avg_vol_20) * 100, 1)}%"
                })
        except Exception:
            pass

    if ready_targets:
        df_out = pd.DataFrame(ready_targets)
        ws_ready.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Filtered {len(ready_targets)} Volume Dry stocks and saved to 'Ready_For_Today'!")
    else:
        print("ℹ️ No Volume Dry setups matched today.")

# =====================================================================
# STEP 2: INTRADAY LIVE SCAN (Market Hours)
# Reads ONLY 'Ready_For_Today' sheet -> Tests Live Volume & Price Breakout
# =====================================================================
else:
    print("⚡ Running INTRADAY LIVE BREAKOUT TEST on 'Ready_For_Today'...", flush=True)

    try:
        ws_ready = sh.worksheet("Ready_For_Today")
        records = ws_ready.get_all_records()
        if not records:
            print("❌ No target stocks found in 'Ready_For_Today' tab.")
            exit(0)
        df_ready = pd.DataFrame(records)
    except Exception as e:
        print(f"❌ Error reading 'Ready_For_Today': {e}")
        exit(1)

    try:
        ws_live = sh.worksheet("LIVE_BREAKOUTS")
        ws_live.clear()
    except Exception:
        ws_live = sh.add_worksheet(title="LIVE_BREAKOUTS", rows="100", cols="10")

    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    mins_passed = max(int((now - market_start).total_seconds() / 60), 5)
    projected_factor = 375 / mins_passed

    confirmed_breakouts = []

    for _, row in df_ready.iterrows():
        symbol = str(row['Stock']).strip()
        if not symbol.endswith(".NS"):
            symbol += ".NS"

        trigger_high = float(row['Trigger_High'])
        v_sma20 = float(row['Vol_SMA20'])

        try:
            ticker = yf.Ticker(symbol)
            df_live = ticker.history(period="1d", interval="5m")

            if df_live.empty:
                continue

            live_price = round(float(df_live['Close'].iloc[-1]), 2)
            current_vol = float(df_live['Volume'].sum())

            projected_vol = current_vol * projected_factor
            vol_ratio = round(projected_vol / v_sma20, 2) if v_sma20 > 0 else 0.0

            # LIVE TRIGGER: Price crosses Previous Day High AND Projected Volume Surge >= 1.8x
            if live_price >= trigger_high and vol_ratio >= 1.8:
                pct_change = round(((live_price - trigger_high) / trigger_high) * 100, 2)
                confirmed_breakouts.append({
                    "Stock": symbol.replace(".NS", ""),
                    "Live_Price": live_price,
                    "Trigger_High": trigger_high,
                    "Gain_%": pct_change,
                    "Projected_Vol": f"{vol_ratio}x",
                    "Scan_Time": now.strftime('%H:%M IST')
                })
                print(f"🚀 BREAKOUT DETECTED: {symbol} | Price: {live_price} | Vol: {vol_ratio}x")

        except Exception:
            pass

    if confirmed_breakouts:
        df_out = pd.DataFrame(confirmed_breakouts)
        ws_live.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Pushed {len(confirmed_breakouts)} live breakout alerts to 'LIVE_BREAKOUTS'!")
    else:
        ws_live.update([
            ["Stock", "Live_Price", "Trigger_High", "Gain_%", "Projected_Vol", "Scan_Time"],
            ["NO BREAKOUT YET", "-", "-", "-", "-", now.strftime('%H:%M IST')]
        ])
        print("ℹ️ No volume surge breakouts matched at this time.")
