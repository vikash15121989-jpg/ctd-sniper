import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V8.5 - HYBRID BALANCED ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL - LOOSE =====
F = {
    'min_market_cap_cr': 500,
    'max_debt_equity': 10.0,
    'max_pe': 1000,
}

# ===== 3. TECHNICAL - V8.5 HYBRID =====
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'sl_buffer_pct': 3.0,
    'target_r': 1.0,
    'max_risk_pct': 35.0, # 35% cap - V8.2 aur V8.3 ke beech
    'vol_blast_ratio': 1.2,
    'rs_days': 45, # V8.2 wala 45d
}

debug_fund = []
debug_tech = []

# Nifty data - FIXED VERSION
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)

nifty['52W_High'] = nifty['High'].rolling(252).max()
# FIX: idxmax se date nikal, apply se nahi
nifty['52W_High_Date'] = nifty['High'].rolling(252).apply(lambda x: x.idxmax() if len(x) == 252 else pd.NaT)

def get_fundamentals_v8_5(stock):
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['debt_equity'] = info.get('debtToEquity', 999)
        fund_data['pe'] = info.get('trailingPE', 999)

        if fund_data['market_cap_cr'] < F['min_market_cap_cr']:
            return False, fund_data, f"Mcap {fund_data['market_cap_cr']}Cr < 500"
        if fund_data['debt_equity'] > F['max_debt_equity']:
            return False, fund_data, f"DE {fund_data['debt_equity']} > 10"
        if fund_data['pe'] > F['max_pe']:
            return False, fund_data, f"PE {fund_data['pe']} > 1000"
        return True, fund_data, "PASS"
    except:
        return False, fund_data, "Error"

def check_52w_high_breakout_v8_5(df, idx):
    if idx < 252: return False, {}, "Data < 252"

    row = df.iloc[idx]
    entry_date = df.index[idx]

    if row['Close'] < R['min_price']:
        return False, {}, f"Price {row['Close']} < 50"

    avg_value_cr = (df['Close'].iloc[idx-20:idx] * df['Volume'].iloc[idx-20:idx]).mean() / 1e7
    if avg_value_cr < R['min_daily_value_cr']:
        return False, {}, f"Liquidity {avg_value_cr:.2f}Cr < 0.5"

    high_252 = df['High'].iloc[idx-252:idx].max()
    if row['Close'] <= high_252 * 1.01:
        return False, {}, "No 52W BO +1%"

    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0: avg_vol_20 = 1
    vol_ratio = row['Volume'] / avg_vol_20
    if vol_ratio < R['vol_blast_ratio']:
        return False, {}, f"Vol {vol_ratio:.1f}x < 1.2x"

    try:
        nifty_52h_date = nifty['52W_High_Date'].loc[entry_date]
        if pd.isna(nifty_52h_date):
            return False, {}, "Nifty 52W Date NaN"
        days_diff = (entry_date - nifty_52h_date).days
        if abs(days_diff) > R['rs_days']:
            return False, {}, f"RS {days_diff} days > 45"
    except Exception as e:
        return False, {}, f"RS Error: {str(e)[:20]}"

    return True, {
        'bo_date': entry_date.strftime('%Y-%m-%d'),
        'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2),
        'vol_blast_x': round(vol_ratio, 1),
        'rs_days_diff': days_diff,
        'nifty_52w_date': nifty_52h_date.strftime('%Y-%m-%d'),
        'liquidity_cr': round(avg_value_cr, 2)
    }, "V8.5 BO PASS"

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def scan_stock_v8_5(stock):
    global debug_fund, debug_tech

    fund_pass, fund_data, fund_reason = get_fundamentals_v8_5(stock)
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
            is_bo, bo_data, bo_reason = check_52w_high_breakout_v8_5(df, i)
            if is_bo:
                entry_price = df['Close'].iloc[i]
                sl_price = df['Low'].iloc[i-20:i+1].min() * 0.97
                risk = entry_price - sl_price
                risk_pct = risk / entry_price * 100

                if risk_pct > R['max_risk_pct'] or risk_pct <= 0:
                    debug_tech.append({'Stock': stock, 'Reason': f'Risk {risk_pct:.1f}% > 35%'})
                    continue

                target = entry_price + risk * R['target_r']
                result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

                entry_data = {
                    'Stock': stock, 'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                    'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
                    'Result': result, 'Exit_Date': exit_date, 'PnL_%': pnl,
                    '52W_High': bo_data['52w_high'], 'Vol_Blast_X': bo_data['vol_blast_x'],
                    'RS_Days_Diff': bo_data['rs_days_diff'], 'Nifty_52W_Date': bo_data['nifty_52w_date'],
                    'Liquidity_Cr': bo_data['liquidity_cr'], **fund_data
                }
                results.append(entry_data)
                debug_tech.append({'Stock': stock, 'Reason': f'V8.5 BO FOUND'})
                break

        if not results:
            debug_tech.append({'Stock': stock, 'Reason': 'No V8.5 BO'})

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:40]}'})
        return []

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks - V8.5 HYBRID MODE...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v8_5(stock)
    all_results.extend(trades)
    if i % 50 == 0 or i == len(stocks) - 1:
        fund_count = len([d for d in debug_fund if d['Pass']])
        print(f"Done {i+1}/{len(stocks)} | Fund Pass: {fund_count} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(debug_fund)
df_tech = pd.DataFrame(debug_tech)

if not all_results:
    print("0 SETUP MILA")
    wins = total = total_pnl = avg_win = avg_loss = max_drawdown = 0
    winrate = 0
    df_res = pd.DataFrame()
else:
    df_res = pd.DataFrame(all_results).sort_values('Entry_Date')
    total = len(df_res)
    wins = len(df_res[df_res['Result'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_res['PnL_%'].sum()
    avg_win = df_res[df_res['Result'] == 'WIN']['PnL_%'].mean() if wins > 0 else 0
    avg_loss = df_res[df_res['Result'] == 'LOSS']['PnL_%'].mean() if len(df_res[df_res['Result'] == 'LOSS']) > 0 else 0
    max_drawdown = df_res['PnL_%'].cumsum().min()

summary = pd.DataFrame([{
    'Total_Stocks': len(stocks),
    'Fund_Pass': len([d for d in debug_fund if d['Pass']]),
    'Total_Setups': len(all_results),
    'Winrate_%': winrate if all_results else 0,
    'Avg_Win_%': round(avg_win, 1) if wins > 0 else 0,
    'Avg_Loss_%': round(avg_loss, 1) if len(df_res[df_res['Result'] == 'LOSS']) > 0 else 0,
    'Total_PnL_%': round(total_pnl, 1) if all_results else 0,
    'Max_Drawdown_%': round(max_drawdown, 1) if all_results else 0,
    'Strategy': 'V8.5 HYBRID'
}])

# ===== GSHEET UPDATE =====
def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        sh.del_worksheet(ws)
        print(f"Deleted old {sheet_name}", flush=True)
    except gspread.exceptions.WorksheetNotFound:
        pass

    rows = len(df) + 20 if not df.empty else 100
    cols = len(df.columns) + 5 if not df.empty else 26
    ws = sh.add_worksheet(title=sheet_name, rows=rows, cols=cols)

    if not df.empty:
        payload = [df.columns.values.tolist()] + df.fillna('').values.tolist()
        ws.update('A1', payload, value_input_option='USER_ENTERED')
        print(f"Created {sheet_name} with {len(df)} rows", flush=True)

update_gsheet('DEBUG_FUNDAMENTAL_V8_5', df_fund)
update_gsheet('DEBUG_TECHNICAL_V8_5', df_tech)
if all_results:
    update_gsheet('QFACTOR_V8_5_TRADES', df_res)
update_gsheet('QFACTOR_V8_5_SUMMARY', summary)

print(f"\n=== V8.5 COMPLETE ===", flush=True)
print(f"Fund Pass: {len([d for d in debug_fund if d['Pass']])} | Setups: {len(all_results)}", flush=True)
if all_results:
    print(f"Winrate: {winrate}% | Total PnL: {total_pnl:.1f}%", flush=True)
print(f"Check QFACTOR_V8_5_SUMMARY sheet", flush=True)
