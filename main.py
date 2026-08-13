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
# MODE 1: NIGHTLY EOD SCAN (Strict Single Bar Validation)
# =====================================================================
if current_hour >= 16 or current_hour < 8:
    print("📌 Running NIGHTLY EOD SCAN with Strict Data Fix...", flush=True)
    
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
            # Ticker history prevents MultiIndex bugs
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="60d", interval="1d")

            if df.empty or len(df) < 25:
                continue

            # Calculate 20 EMA
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()

            # Strictly pick last TWO COMPLETED daily bars
            # If scanning after 16:00 IST, iloc[-2] is Yesterday and iloc[-1] is Today
            mother_bar = df.iloc[-2]
            inside_bar = df.iloc[-1]

            mother_high = float(mother_bar['High'])
            mother_low = float(mother_bar['Low'])
            inside_high = float(inside_bar['High'])
            inside_low = float(inside_bar['Low'])
            
            avg_vol_20 = float(df['Volume'].iloc[-21:-1].mean())
            close = float(inside_bar['Close'])
            ema20 = float(inside_bar['EMA20'])
            
            avg_daily_value = avg_vol_20 * close

            # Filter 1: Avg Volume >= 10 Lakh
            if avg_vol_20 < 1000000:
                continue

            # Filter 2: Avg Daily Value >= 3 Cr
            if avg_daily_value < 30000000:
                continue

            # Filter 3: Price > 20 EMA
            if close <= ema20:
                continue

            # Filter 4: STRICT INSIDE BAR (Inside High < Mother High AND Inside Low > Mother Low)
            is_inside = (inside_high < mother_high) and (inside_low > mother_low)

            if is_inside:
                targets.append({
                    "Stock": symbol.replace(".NS", ""),
                    "Mother_High": round(mother_high, 2),
                    "Mother_Low": round(mother_low, 2),
                    "Vol_SMA20": int(avg_vol_20),
                    "Traded_Val_Cr": round(avg_daily_value / 10000000, 2),
                    "EMA20": round(ema20, 2),
                    "Last_Close": round(close, 2)
                })
        except Exception:
            pass

    if targets:
        df_out = pd.DataFrame(targets)
        ws_targets.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Successfully pushed {len(targets)} STRICT targets to 'Today_Targets'!")
    else:
        print("ℹ️ No Inside Bar targets matched today.")

# =====================================================================
# MODE 2: INTRADAY LIVE SCAN (Market Hours)
# =====================================================================
else:
    print("⚡ Running INTRADAY LIVE SCAN...", flush=True)
    
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
            ticker = yf.Ticker(symbol)
            df_live = ticker.history(period="1d", interval="5m")

            if df_live.empty:
                continue

            live_price = round(float(df_live['Close'].iloc[-1]), 2)
            current_vol = float(df_live['Volume'].sum())

            projected_vol = current_vol * projected_factor
            vol_ratio = round(projected_vol / v_sma20, 2) if v_sma20 > 0 else 0.0

            # TRIGGER CONDITION: Price >= Mother High AND Projected Vol >= 1.2x
            if live_price >= m_high and vol_ratio >= 1.2:
                pct_change = round(((live_price - m_high) / m_high) * 100, 2)
                confirmed_breakouts.append({
                    "Stock": symbol.replace(".NS", ""),
                    "Live_Price": live_price,
                    "Mother_High": m_high,
                    "Gain_Vs_High_%": pct_change,
                    "Projected_Vol": f"{vol_ratio}x",
                    "Scan_Time": now.strftime('%H:%M IST')
                })
                print(f"🔥 BREAKOUT: {symbol} | Price: {live_price} | Vol: {vol_ratio}x")

        except Exception:
            pass

    if confirmed_breakouts:
        df_out = pd.DataFrame(confirmed_breakouts)
        ws_live.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"✅ Successfully written {len(confirmed_breakouts)} breakout stocks to sheet!")
    else:
        ws_live.update([["Stock", "Live_Price", "Mother_High", "Gain_Vs_High_%", "Projected_Vol", "Scan_Time"], ["NO BREAKOUT YET", "-", "-", "-", "-", now.strftime('%H:%M IST')]])
        print("ℹ️ No volume surge breakouts matched at this time.")
        
