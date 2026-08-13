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
# MODE 1: NIGHTLY EOD SCAN (Strict Volume & Traded Value Filters)
# =====================================================================
if current_hour >= 16 or current_hour < 8:
    print("📌 Running NIGHTLY EOD SCAN with 10 Lakh Vol & 3 Cr Value Filters...", flush=True)
    
    try:
        ws_targets = sh.worksheet("Today_Targets")
        ws_targets.clear()
    except Exception:
        ws_targets = sh.add_worksheet(title="Today_Targets", rows="100", cols="10")

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

    targets = []
    for symbol in STOCKS:
        try:
            df = yf.download(symbol, period="30d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            if len(df) < 22:
                continue

            # Extract exact values
            mother_high = float(df['High'].iloc[-2])
            mother_low = float(df['Low'].iloc[-2])
            inside_high = float(df['High'].iloc[-1])
            inside_low = float(df['Low'].iloc[-1])
            
            # 20-Day Average Volume
            avg_vol_20 = float(df['Volume'].iloc[-21:-1].mean())
            close = float(df['Close'].iloc[-1])
            
            # Average Daily Traded Value (in Rupees)
            avg_daily_value = avg_vol_20 * close

            # --- FILTER 1: Average Volume >= 10 Lakhs (1,000,000 shares) ---
            if avg_vol_20 < 1000000:
                continue

            # --- FILTER 2: Average Daily Traded Value >= ₹3 Crore (30,000,000 INR) ---
            if avg_daily_value < 30000000:
                continue

            # --- FILTER 3: Inside Bar Compression Check ---
            is_inside = (inside_high < mother_high) and (inside_low > mother_low)

            if is_inside:
                targets.append({
                    "Stock": symbol.replace(".NS", ""),
                    "Mother_High": round(mother_high, 2),
                    "Mother_Low": round(mother_low, 2),
                    "Vol_SMA20": int(avg_vol_20),
                    "Traded_Val_Cr": round(avg_daily_value / 10000000, 2), # Value in Crores
                    "Last_Close": round(close, 2)
                })
        except Exception:
            pass

    if targets:
        df_out = pd.DataFrame(targets)
        ws_targets.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Successfully pushed {len(targets)} highly liquid targets to 'Today_Targets'!")
    else:
        print("ℹ️ No Inside Bar targets matched the liquidity criteria today.")

# =====================================================================
# MODE 2: INTRADAY LIVE SCAN (Subah 9:30 AM Market Hours)
# =====================================================================
else:
    print("⚡ Running INTRADAY LIVE VOLUME SURGE SCAN...", flush=True)
    
    try:
        ws_targets = sh.worksheet("Today_Targets")
        records = ws_targets.get_all_records()
        if not records:
            print("❌ No target stocks found in 'Today_Targets' tab.")
            exit(0)
        df_targets = pd.DataFrame(records)
    except Exception as e:
        print(f"❌ Error reading Today_Targets: {e}")
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

    for _, row in df_targets.iterrows():
        symbol = str(row['Stock']).strip()
        if not symbol.endswith(".NS"):
            symbol += ".NS"
            
        m_high = float(row['Mother_High'])
        v_sma20 = float(row['Vol_SMA20'])

        try:
            df_live = yf.download(symbol, period="1d", interval="5m", progress=False)
            if isinstance(df_live.columns, pd.MultiIndex):
                df_live.columns = df_live.columns.get_level_values(0)

            if df_live.empty:
                continue

            live_price = round(float(df_live['Close'].iloc[-1]), 2)
            current_vol = float(df_live['Volume'].sum())

            projected_vol = current_vol * projected_factor
            vol_ratio = round(projected_vol / v_sma20, 2) if v_sma20 > 0 else 0.0

            # TRIGGER CONDITION: Price >= Mother High AND Projected Vol >= 2.0x
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

    if confirmed_breakouts:
        df_out = pd.DataFrame(confirmed_breakouts)
        ws_live.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Successfully wrote {len(confirmed_breakouts)} breakout stocks to 'LIVE_BREAKOUTS' sheet!")
    else:
        ws_live.update([["Stock", "Live_Price", "Mother_High", "Projected_Vol_Ratio", "Scan_Time"], ["NO BREAKOUT YET", "-", "-", "-", now.strftime('%H:%M IST')]])
        print("ℹ️ No volume surge breakouts matched at this time.")
        
