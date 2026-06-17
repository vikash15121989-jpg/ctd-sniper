import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V8.6 - RS WINDOW VALIDATION ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL =====
F = {'min_market_cap_cr': 500, 'max_debt_equity': 10.0, 'max_pe': 1000}

# ===== 3. TECHNICAL - V8.6 RS WINDOW =====
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'sl_buffer_pct': 3.0,
    'target_r': 1.0,
    'max_risk_pct': 25.0,
    'vol_blast_ratio': 1.2,
    'rs_days': 30, # First RS prove ke liye
}

debug_fund = []
debug_tech = []

# Nifty data + 52W High dates
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)

nifty['52W_High'] = nifty['High'].rolling(252).max()
# Nifty ke har 52W high ki date nikal lo
nifty_52h_dates = []
last_high = 0
for i in range(252, len(nifty)):
    if nifty['High'].iloc[i] > last_high:
        last_high = nifty['High'].iloc[i]
        nifty_52h_dates.append(nifty.index[i])
    last_high = max(last_high, nifty['High'].iloc[i])

print(f"Nifty 52W Highs found: {len(nifty_52h_dates)}", flush=True)

def get_fundamentals_v8_6(stock):
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['debt_equity'] = info.get('debtToEquity', 999)
        fund_data['pe'] = info.get('trailingPE', 999)
        if fund_data['market_cap_cr'] < F['min_market_cap_cr']: return False, fund_data, "Mcap fail"
        if fund_data['debt_equity'] > F['max_debt_equity']: return False, fund_data, "DE fail"
        if fund_data['pe'] > F['max_pe']: return False, fund_data, "PE fail"
        return True, fund_data, "PASS"
    except:
        return False, fund_data, "Error"

def scan_stock_v8_6(stock):
    global debug_fund, debug_tech
    fund_pass, fund_data, fund_reason = get_fundamentals_v8_6(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) < 300: return []
        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100: return []

        results = []
        rs_valid_start = None # Kab se RS valid hai
        rs_valid_end = None # Kab tak RS valid hai
        last_entry_idx = -100

        for i in range(252, len(df)):
            if i - last_entry_idx < 60: continue # 60 din cooldown
            row = df.iloc[i]
            entry_date = df.index[i]

            # Basic filters
            if row['Close'] < R['min_price']: continue
            avg_value_cr = (df['Close'].iloc[i-20:i] * df['Volume'].iloc[i-20:i]).mean() / 1e7
            if avg_value_cr < R['min_daily_value_cr']: continue

            # Aaj 52W BO hai ya nahi
            high_252 = df['High'].iloc[i-252:i].max()
            is_bo_today = row['Close'] > high_252 * 1.01 and df['Close'].iloc[i-1] <= high_252 * 1.01
            if not is_bo_today: continue

            # Volume blast
            avg_vol_20 = df['Volume'].iloc[i-20:i].mean()
            if avg_vol_20 == 0: avg_vol_20 = 1
            vol_ratio = row['Volume'] / avg_vol_20
            if vol_ratio < R['vol_blast_ratio']: continue

            # === V8.6 RS WINDOW CHECK ===
            entry_type = None

            # 1. Check karo kya aaj First Time RS BO hai?
            for nifty_date in reversed(nifty_52h_dates): # Latest se check karo
                if nifty_date <= entry_date:
                    days_diff = (entry_date - nifty_date).days
                    if abs(days_diff) <= R['rs_days']:
                        entry_type = "FIRST_RS_BO"
                        # Naya RS window set karo
                        rs_valid_start = entry_date
                        # Agla nifty high kab aayega
                        next_highs = [d for d in nifty_52h_dates if d > nifty_date]
                        rs_valid_end = next_highs[0] if next_highs else BACKTEST_END
                        break
                    else:
                        break # Purane nifty high se 30 din se zyada

            # 2. Agar First BO nahi hai, to check karo kya RS Window me hai?
            if not entry_type and rs_valid_start and entry_date >= rs_valid_start and entry_date < rs_valid_end:
                entry_type = "RS_WINDOW_BO"

            if not entry_type: continue
            # === V8.6 LOGIC END ===

            # Risk check
            entry_price = row['Close']
            sl_price = df['Low'].iloc[i-20:i+1].min() * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue

            target = entry_price + risk * R['target_r']
            result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

            entry_data = {
                'Stock': stock, 'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                'Entry_Type': entry_type, 'Entry': round(entry_price, 2),
                'SL': round(sl_price, 2), 'Target': round(target, 2),
                'Risk_%': round(risk_pct, 1), 'Result': result,
                'Exit_Date': exit_date, 'PnL_%': pnl,
                'RS_Valid_From': rs_valid_start.strftime('%Y-%m-%d') if rs_valid_start else '',
                'RS_Valid_Till': rs_valid_end.strftime('%Y-%m-%d') if rs_valid_end else '',
                '52W_High': round(high_252, 2), 'Vol_X': round(vol_ratio, 1),
                **fund_data
            }
            results.append(entry_data)
            last_entry_idx = i

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:40]}'})
        return []

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl: return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target: return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

# ===== MAIN =====
for attempt in range(3):
    try:
        stocks = ws_watchlist.col_values(1)[1:]
        stocks = [s.strip().upper() for s in stocks if s.strip()]
        print(f"Loaded {len(stocks)} stocks", flush=True)
        break
    except gspread.exceptions.APIError:
        if attempt < 2: time.sleep(10)
        else: raise

print(f"Scanning {len(stocks)} stocks - V8.6 RS WINDOW MODE...", flush=True)
all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v8_6(stock)
    all_results.extend(trades)
    if i % 50 == 0: print(f"Done {i+1}/{len(stocks)} | Setups: {len(all_results)}", flush=True)

# Summary same as V8.5...
