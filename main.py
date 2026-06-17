import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V9 - TIGHT FILTERS ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL - TIGHT =====
F = {
    'min_market_cap_cr': 500, # 300 se 500 kiya
    'max_debt_equity': 2.0, # 3.0 se 2.0 kiya
    'max_pe': 200, # 500 se 200 kiya
}

# ===== 3. TECHNICAL - V9 TIGHT =====
R = {
    'min_price': 50, # 5 se 50 kiya - liquidity
    'min_daily_value_cr': 0.5, # 0.02 se 0.5 kiya
    'sl_buffer_pct': 3.0,
    'target_r': 1.5, # 1.2 se 1.5 kiya - profit badhao
    'max_risk_pct': 30.0, # 40 se 30 kiya - safety
    'min_rr_pct': 3.0, # 2.0 se 3.0 kiya
    'vol_blast_ratio': 2.0, # 1.2 se 2.0 kiya - strong volume
    'rs_days': 15, # 30 se 15 kiya - tight RS
}

debug_fund = []
debug_tech = []

# Nifty data
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)

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

def get_fundamentals_v9(stock):
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['debt_equity'] = info.get('debtToEquity', 999)
        fund_data['pe'] = info.get('trailingPE', 999)

        if fund_data['market_cap_cr'] < F['min_market_cap_cr']:
            return False, fund_data, f"Mcap {fund_data['market_cap_cr']}Cr"
        if fund_data['debt_equity'] > F['max_debt_equity']:
            return False, fund_data, f"DE {fund_data['debt_equity']}"
        if fund_data['pe'] > F['max_pe']:
            return False, fund_data, f"PE {fund_data['pe']}"
        return True, fund_data, "PASS"
    except:
        return False, fund_data, "Error"

def check_52w_high_breakout_v9(df, idx):
    if idx < 252: return False, {}, "Data < 252"

    row = df.iloc[idx]

    # 1. Price filter
    if row['Close'] < R['min_price']:
        return False, {}, f"Price {row['Close']} < 50"

    # 2. Liquidity filter
    avg_value_cr = (df['Close'].iloc[idx-20:idx] * df['Volume'].iloc[idx-20:idx]).mean() / 1e7
    if avg_value_cr < R['min_daily_value_cr']:
        return False, {}, f"Liquidity {avg_value_cr:.2f}Cr low"

    # 3. 52W High BO
    high_252 = df['High'].iloc[idx-252:idx].max()
    if row['Close'] <= high_252 * 1.01:
        return False, {}, "No 52W BO"

    # 4. Volume Blast 2x
    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0: avg_vol_20 = 1
    vol_ratio = row['Volume'] / avg_vol_20
    if vol_ratio < R['vol_blast_ratio']:
        return False, {}, f"Vol {vol_ratio:.1f}x < 2x"

    # 5. RS Tight 15 days
    try:
        nifty_52h_date = nifty.loc[df.index[idx]]['52W_High_Date']
        if pd.isna(nifty_52h_date):
            rs_ok = False
            days_diff = 999
        else:
            days_diff = (df.index[idx] - nifty_52h_date).days
            rs_ok = abs(days_diff) <= R['rs_days']
    except:
        rs_ok = False
        days_diff = 999

    if not rs_ok:
        return False, {}, f"RS {days_diff} days"

    return True, {
        'bo_date': df.index[idx].strftime('%Y-%m-%d'),
        'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2),
        'vol_blast_x': round(vol_ratio, 1),
        'rs_days_diff': days_diff,
        'rs_ok': rs_ok,
        'liquidity_cr': round(avg_value_cr, 2)
    }, "V9 BO PASS"

def scan_stock_v9(stock):
    global debug_fund, debug_tech

    fund_pass, fund_data, fund_reason = get_fundamentals_v9(stock)
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
            is_bo, bo_data, bo_reason = check_52w_high_breakout_v9(df, i)
            if is_bo:
                entry_price = df['Close'].iloc[i]
                sl_price = df['Low'].iloc[i-20:i+1].min() * 0.97
                risk = entry_price - sl_price
                risk_pct = risk / entry_price * 100

                # Risk 30% cap
                if risk_pct > R['max_risk_pct'] or risk_pct <= 0:
                    debug_tech.append({'Stock': stock, 'Reason': f'Risk {risk_pct:.1f}% > 30%'})
                    continue

                # Min RR check
                if risk_pct < R['min_rr_pct']:
                    debug_tech.append({'Stock': stock, 'Reason': f'Risk {risk_pct:.1f}% < 3%'})
                    continue

                target = entry_price + risk * R['target_r']
                result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

                entry_data = {
                    'Stock': stock, 'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                    'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
                    'Result': result, 'Exit_Date': exit_date, 'PnL_%': pnl,
                    '52W_High': bo_data['52w_high'], 'Vol_Blast_X': bo_data['vol_blast_x'],
                    'RS_Days_Diff': bo_data['rs_days_diff'], 'Liquidity_Cr': bo_data['liquidity_cr'],
                    **fund_data
                }
                results.append(entry_data)
                debug_tech.append({'Stock': stock, 'Reason': f'V9 BO FOUND'})
                break

        if not results:
            debug_tech.append({'Stock': stock, 'Reason': 'No V9 BO'})

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
print(f"Scanning {len(stocks)} stocks - V9 TIGHT MODE...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v9(stock)
    all_results.extend(trades)
    if i % 50 == 0 or i == len(stocks) - 1:
        fund_count = len([d for d in debug_fund if d['Pass']])
        print(f"Done {i+1}/{len(stocks)} | Fund Pass: {fund_count} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(debug_fund)
df_tech = pd.DataFrame(debug_tech)

if not all_results:
    print("0 SETUP MILA - FILTER BAHUT TIGHT HAI")
else:
    df_res = pd.DataFrame(all_results).sort_values('Entry_Date')
    total = len(df_res)
    wins = len(df_res[df_res['Result'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_res['PnL_%'].sum()
    avg_win = df_res[df_res['Result'] == 'WIN']['PnL_%'].mean() if wins > 0 else 0
    avg_loss = df_res[df_res['Result'] == 'LOSS']['PnL_%'].mean() if len(df_res[df_res['Result'] == 'LOSS']) > 0 else 0

summary = pd.DataFrame([{
    'Total_Stocks': len(stocks),
    'Fund_Pass': len([d for d in debug_fund if d['Pass']]),
    'Total_Setups': len(all_results),
    'Winrate_%': winrate if all_results else 0,
    'Avg_Win_%': round(avg_win, 1),
    'Avg_Loss_%': round(avg_loss, 1),
    'Total_PnL_%': round(total_pnl, 1) if all_results else 0,
    'Strategy': 'V9 TIGHT'
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

update_gsheet('DEBUG_FUNDAMENTAL_V9', df_fund)
update_gsheet('DEBUG_TECHNICAL_V9', df_tech)
if all_results:
    update_gsheet('QFACTOR_V9_TRADES', df_res)
update_gsheet('QFACTOR_V9_SUMMARY', summary)

print(f"\n=== V9 COMPLETE ===", flush=True)
print(f"Setups: {len(all_results)} | Check QFACTOR_V9_TRADES", flush=True)
