import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V8.2 - 52W HIGH FIXED ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL - BAHUT LOOSE =====
F = {
    'min_market_cap_cr': 300,
    'max_debt_equity': 3.0,
    'max_pe': 500,
}

# ===== 3. TECHNICAL - 52W HIGH LOGIC =====
R = {
    'min_price': 5, 'min_daily_value_cr': 0.02,
    'sl_buffer_pct': 3.0, 'target_r': 1.2,
    'max_risk_pct': 40.0, 'min_rr_pct': 2.0,
    'vol_blast_ratio': 1.2,
}

debug_fund = []
debug_tech = []

# Nifty data - FIXED VERSION: LOOP SE NIKALO, ROLLING NAHI
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)

# YAHAN FIX KIYA - Rolling ki jagah loop
nifty['52W_High'] = nifty['High'].rolling(252).max()
nifty_52w_dates = []
for i in range(len(nifty)):
    if i < 251:
        nifty_52w_dates.append(pd.NaT)
    else:
        window = nifty['High'].iloc[i-251:i+1]
        max_date = window.idxmax()
        nifty_52w_dates.append(max_date)
nifty['52W_High_Date'] = nifty_52w_dates

def get_fundamentals_v8(stock):
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        if fund_data['market_cap_cr'] < F['min_market_cap_cr']:
            return False, fund_data, f"Mcap {fund_data['market_cap_cr']}Cr"
        return True, fund_data, "PASS"
    except:
        return False, fund_data, "Error"

def check_52w_high_breakout(df, idx):
    if idx < 252: return False, {}, "Data < 252"

    row = df.iloc[idx]
    high_252 = df['High'].iloc[idx-252:idx].max()
    if row['Close'] <= high_252 * 1.01:
        return False, {}, "No 52W BO"

    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0: avg_vol_20 = 1
    if row['Volume'] < avg_vol_20 * R['vol_blast_ratio']:
        return False, {}, "No Vol Blast"

    # Nifty RS check - safe
    try:
        nifty_52h_date = nifty.loc[df.index[idx]]['52W_High_Date']
        if pd.isna(nifty_52h_date):
            rs_ok = False
            days_diff = 999
        else:
            days_diff = (df.index[idx] - nifty_52h_date).days
            rs_ok = abs(days_diff) <= 30
    except:
        rs_ok = False
        days_diff = 999

    return True, {
        'bo_date': df.index[idx].strftime('%Y-%m-%d'),
        'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2),
        'vol_blast_x': round(row['Volume'] / avg_vol_20, 1),
        'rs_days_diff': days_diff,
        'rs_ok': rs_ok
    }, "52W BO PASS"

def scan_stock_v8(stock):
    global debug_fund, debug_tech

    fund_pass, fund_data, fund_reason = get_fundamentals_v8(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=15)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 300:
            debug_tech.append({'Stock': stock, 'Reason': 'Data < 300'})
            return []

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100:
            debug_tech.append({'Stock': stock, 'Reason': 'Data < 100'})
            return []

        results = []

        for i in range(252, len(df)):
            if df['Close'].iloc[i] < R['min_price']: continue

            is_bo, bo_data, bo_reason = check_52w_high_breakout(df, i)
            if is_bo:
                entry_price = df['Close'].iloc[i]
                sl_price = df['Low'].iloc[i-20:i+1].min() * 0.97
                risk = entry_price - sl_price
                risk_pct = risk / entry_price * 100

                if risk_pct > R['max_risk_pct'] or risk_pct <= 0:
                    debug_tech.append({'Stock': stock, 'Reason': f'Risk {risk_pct:.1f}% high'})
                    continue

                target = entry_price + risk * R['target_r']
                result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

                entry_data = {
                    'Stock': stock, 'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                    'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
                    'Result': result, 'Exit_Date': exit_date, 'PnL_%': pnl,
                    '52W_High': bo_data['52w_high'], 'Vol_Blast_X': bo_data['vol_blast_x'],
                    'RS_Days_Diff': bo_data['rs_days_diff'],
                    **fund_data
                }
                results.append(entry_data)
                debug_tech.append({'Stock': stock, 'Reason': f'52W BO FOUND RS:{bo_data["rs_ok"]}'})
                break

        if not results:
            debug_tech.append({'Stock': stock, 'Reason': 'No 52W BO'})

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:40]}'})
        return []

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks - 52W HIGH MODE...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v8(stock)
    all_results.extend(trades)
    if i % 50 == 0 or i == len(stocks) - 1:
        fund_count = len([d for d in debug_fund if d['Pass']])
        print(f"Done {i+1}/{len(stocks)} | Fund Pass: {fund_count} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(debug_fund)
df_tech = pd.DataFrame(debug_tech)

if not all_results:
    print("0 SETUP MILA - BEAR me bhi 52W high nahi bana")
else:
    df_res = pd.DataFrame(all_results).sort_values('Entry_Date')
    total = len(df_res)
    wins = len(df_res[df_res['Result'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_res['PnL_%'].sum()

summary = pd.DataFrame([{
    'Total_Stocks': len(stocks),
    'Fund_Pass': len([d for d in debug_fund if d['Pass']]),
    'Total_Setups': len(all_results),
    'Winrate_%': winrate if all_results else 0,
    'Total_PnL_%': round(total_pnl, 1) if all_results else 0,
    'Strategy': 'V8.2 52W HIGH FIXED'
}])

def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=20000, cols=70)
        ws.clear()
    if not df.empty:
        payload = [df.columns.values.tolist()] + df.fillna('').values.tolist()
        ws.update('A1', payload)

update_gsheet('DEBUG_FUNDAMENTAL', df_fund)
update_gsheet('DEBUG_TECHNICAL', df_tech)
if all_results:
    update_gsheet('QFACTOR_V8_TRADES', df_res)
update_gsheet('QFACTOR_V8_SUMMARY', summary)

print(f"\n=== COMPLETE ===", flush=True)
print(f"Setups: {len(all_results)} | Check QFACTOR_V8_TRADES", flush=True)
