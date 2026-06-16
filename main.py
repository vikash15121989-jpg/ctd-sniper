import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V5 - RELAXED + DEBUG ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. RELAXED FUNDAMENTAL RULES =====
F = {
    'min_market_cap_cr': 1000, # 2000 se 1000 kiya
    'max_debt_equity': 1.0, # 0.5 se 1.0 kiya - BEAR me thoda debt chalta
    'max_beta': 1.2, # 0.9 se 1.2 kiya
    'min_roe': 12.0, # 18 se 12 kiya
    'min_roce': 12.0, # 18 se 12 kiya
    'min_eps_cagr_5y': 5.0, # 12 se 5 kiya - BEAR me growth kam
    'max_pe': 80, # 60 se 80 kiya
}

# ===== 3. TECHNICAL RULES - RELAXED =====
R = {
    'base_min_days': 20, 'base_max_days': 90, 'base_range_max_pct': 25.0, # Range badhayi
    'base_vol_dry_pct': 0.60, 'bo_vol_spike': 1.8, 'bo_buffer_pct': 1.0, # BO easy kiya
    'retest_vol_max_pct': 0.50, 'retest_zone_pct': 5.0, # Retest zone badhaya
    'sl_buffer_pct': 1.0, 'target_r': 1.5, # Target 2 se 1.5 kiya
    'min_price': 30, 'min_daily_value_cr': 0.2, # Liquidity kam kiya
    'max_risk_pct': 20.0, 'min_rr_pct': 5.0,
}

debug_fund = []
debug_tech = []

def get_fundamentals_debug(stock):
    """Fundamental + Debug reason"""
    fund_data = {'stock': stock}
    reason = ""
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info

        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['beta'] = round(info.get('beta', 99), 2)
        fund_data['pe'] = round(info.get('trailingPE', 999), 1)

        if fund_data['market_cap_cr'] < F['min_market_cap_cr']:
            reason = f"Mcap {fund_data['market_cap_cr']}Cr < {F['min_market_cap_cr']}"
            return False, fund_data, reason
        if fund_data['beta'] > F['max_beta']:
            reason = f"Beta {fund_data['beta']} > {F['max_beta']}"
            return False, fund_data, reason

        # Balance Sheet
        bs = t.balance_sheet
        if not bs.empty:
            debt = bs.loc['Total Debt'].iloc[0] if 'Total Debt' in bs.index else 0
            equity = bs.loc['Total Stockholder Equity'].iloc[0] if 'Total Stockholder Equity' in bs.index else 1
            fund_data['debt_equity'] = round(debt / equity, 2) if equity else 99
            if fund_data['debt_equity'] > F['max_debt_equity']:
                reason = f"D/E {fund_data['debt_equity']} > {F['max_debt_equity']}"
                return False, fund_data, reason

        # Financials
        fin = t.financials
        if not fin.empty and len(fin.columns) >= 2:
            net_income = fin.loc['Net Income'] if 'Net Income' in fin.index else pd.Series()
            ebit = fin.loc['EBIT'] if 'EBIT' in fin.index else pd.Series()

            if not net_income.empty and not bs.empty:
                avg_equity = bs.loc['Total Stockholder Equity'].iloc[:2].mean() if 'Total Stockholder Equity' in bs.index else 1
                fund_data['roe'] = round(net_income.iloc[0] / avg_equity * 100, 1) if avg_equity else 0
                if fund_data['roe'] < F['min_roe']:
                    reason = f"ROE {fund_data['roe']}% < {F['min_roe']}%"
                    return False, fund_data, reason

            if not ebit.empty and not bs.empty:
                total_assets = bs.loc['Total Assets'].iloc[0] if 'Total Assets' in bs.index else 0
                curr_liab = bs.loc['Current Liabilities'].iloc[0] if 'Current Liabilities' in bs.index else 0
                capital_employed = total_assets - curr_liab
                fund_data['roce'] = round(ebit.iloc[0] / capital_employed * 100, 1) if capital_employed else 0
                if fund_data['roce'] < F['min_roce']:
                    reason = f"ROCE {fund_data['roce']}% < {F['min_roce']}%"
                    return False, fund_data, reason

            if len(net_income) >= 3:
                oldest = net_income.iloc[-1]
                latest = net_income.iloc[0]
                years = len(net_income) - 1
                if oldest > 0 and years > 0:
                    fund_data['eps_cagr_5y'] = round(((latest / oldest) ** (1/years) - 1) * 100, 1)
                    if fund_data['eps_cagr_5y'] < F['min_eps_cagr_5y']:
                        reason = f"EPS CAGR {fund_data['eps_cagr_5y']}% < {F['min_eps_cagr_5y']}%"
                        return False, fund_data, reason

        fund_data['fund_pass'] = True
        return True, fund_data, "PASS"
    except Exception as e:
        reason = f"Error: {str(e)[:50]}"
        return False, fund_data, reason

def add_indicators(df):
    df['Vol_50MA'] = df['Volume'].rolling(50).mean()
    df['Daily_Value_20MA'] = (df['Close'] * df['Volume']).rolling(20).mean()
    df['50DMA'] = df['Close'].rolling(50).mean()
    df['200DMA'] = df['Close'].rolling(200).mean()
    return df

def check_liquidity(df, idx):
    try:
        if df['Close'].iloc[idx] < R['min_price']: return False
        if df['Daily_Value_20MA'].iloc[idx] < R['min_daily_value_cr'] * 1e7: return False
        return True
    except: return False

def find_base_and_breakout(df, idx, stock):
    row = df.iloc[idx]
    tech_reason = ""

    if row['50DMA'] < row['200DMA']:
        tech_reason = "50DMA < 200DMA - Downtrend"
        return False, {}, tech_reason
    if row['Close'] < row['50DMA']:
        tech_reason = "Close < 50DMA"
        return False, {}, tech_reason

    for base_days in range(R['base_min_days'], R['base_max_days'] + 1):
        if idx - base_days < 0: continue
        base_df = df.iloc[idx-base_days:idx]
        base_high = base_df['High'].max()
        base_low = base_df['Low'].min()
        base_range = (base_high - base_low) / base_low * 100
        if base_range > R['base_range_max_pct']: continue
        avg_base_vol = base_df['Volume'].mean()
        if avg_base_vol > row['Vol_50MA'] * R['base_vol_dry_pct']: continue
        bo_level = base_high * (1 + R['bo_buffer_pct']/100)
        if row['Close'] <= bo_level: continue
        if row['Close'] <= row['Open']: continue
        if row['Volume'] < row['Vol_50MA'] * R['bo_vol_spike']: continue

        return True, {
            'bo_date': df.index[idx].strftime('%Y-%m-%d'),
            'bo_level': round(base_high, 2),
            'bo_vol': row['Volume'],
            'bo_idx': idx
        }, "BO PASS"

    tech_reason = "No BO - Range/Vol/Trend fail"
    return False, {}, tech_reason

def check_retest_entry(df, idx, bo_data):
    if idx <= bo_data['bo_idx'] + 1: return False, {}, "No data after BO"
    row = df.iloc[idx]
    bo_level = bo_data['bo_level']
    bo_vol = bo_data['bo_vol']

    zone_low = bo_level * (1 - R['retest_zone_pct']/100)
    zone_high = bo_level * (1 + R['retest_zone_pct']/100)
    if not (zone_low <= row['Low'] <= zone_high): return False, {}, "Not in Retest Zone"
    if row['Volume'] > bo_vol * R['retest_vol_max_pct']: return False, {}, "Retest Vol High"
    if row['Close'] < row['Open'] * 0.995: return False, {}, "Big Red Candle"

    swing_low = df['Low'].iloc[idx-5:idx+1].min()
    sl_price = swing_low * (1 - R['sl_buffer_pct']/100)
    risk = row['Close'] - sl_price
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['max_risk_pct'] or risk_pct <= 0: return False, {}, f"Risk {risk_pct}% > {R['max_risk_pct']}%"

    target = row['Close'] + risk * R['target_r']
    target_pct = (target - row['Close']) / row['Close'] * 100
    if target_pct < R['min_rr_pct']: return False, {}, f"Reward {target_pct}% < {R['min_rr_pct']}%"

    return True, {
        'Entry_Date': df.index[idx].strftime('%Y-%m-%d'),
        'Entry': round(row['Close'], 2), 'SL': round(sl_price, 2),
        'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
        'Reward_%': round(target_pct, 1), 'RR': R['target_r'],
        'BO_Level': bo_level, 'BO_Date': bo_data['bo_date']
    }, "ENTRY PASS"

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def scan_stock_debug(stock):
    global debug_fund, debug_tech

    # STEP 1: FUNDAMENTAL
    fund_pass, fund_data, fund_reason = get_fundamentals_debug(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

    # STEP 2: TECHNICAL
    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=300),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=15)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200:
            debug_tech.append({'Stock': stock, 'Reason': 'Data < 200 days'})
            return []

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100:
            debug_tech.append({'Stock': stock, 'Reason': 'Data < 100 in period'})
            return []
        df = add_indicators(df)
        results = []
        last_bo_idx = -100
        bo_found = False

        for i in range(100, len(df)):
            if not check_liquidity(df, i): continue
            is_bo, bo_data, bo_reason = find_base_and_breakout(df, i, stock)
            if is_bo and i > last_bo_idx + 20:
                last_bo_idx = i
                bo_found = True
                for j in range(i+1, min(i+21, len(df))):
                    is_entry, entry_data, entry_reason = check_retest_entry(df, j, bo_data)
                    if is_entry:
                        result, exit_date, pnl = simulate_trade(df, j, entry_data['SL'], entry_data['Target'])
                        entry_data.update({
                            'Stock': stock, 'Result': result,
                            'Exit_Date': exit_date, 'PnL_%': pnl,
                            **fund_data
                        })
                        results.append(entry_data)
                        debug_tech.append({'Stock': stock, 'Reason': 'SETUP FOUND'})
                        break
                    else:
                        debug_tech.append({'Stock': stock, 'Reason': f'Retest Fail: {entry_reason}'})
                break

        if not bo_found:
            debug_tech.append({'Stock': stock, 'Reason': 'No BO Found'})

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:50]}'})
        return []

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks - RELAXED MODE...", flush=True)

all_results = []
fund_passed = []
for i, stock in enumerate(stocks):
    trades = scan_stock_debug(stock)
    all_results.extend(trades)
    if i % 10 == 0:
        fund_count = len([d for d in debug_fund if d['Pass']])
        print(f"Done {i}/{len(stocks)} | Fund Pass: {fund_count} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(debug_fund)
df_tech = pd.DataFrame(debug_tech)

if not all_results:
    print("0 SETUP MILA - Debug sheets check karo")
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
    'Winrate_%': winrate,
    'Total_PnL_%': round(total_pnl, 1) if all_results else 0,
}])

def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=20000, cols=60)
        ws.clear()
    if not df.empty:
        payload = [df.columns.values.tolist()] + df.fillna('').values.tolist()
        ws.update('A1', payload)

update_gsheet('DEBUG_FUNDAMENTAL', df_fund)
update_gsheet('DEBUG_TECHNICAL', df_tech)
if all_results:
    update_gsheet('QFACTOR_V5_TRADES', df_res)
update_gsheet('QFACTOR_V5_SUMMARY', summary)

print(f"\n=== COMPLETE ===", flush=True)
print(f"Check DEBUG_FUNDAMENTAL & DEBUG_TECHNICAL sheets for reasons", flush=True)
