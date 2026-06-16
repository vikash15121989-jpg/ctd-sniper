import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V4 - FULL AUTO 1-CLICK ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

print(f"Period: {BACKTEST_START.date()} to {BACKTEST_END.date()}", flush=True)

# ===== 2. FUNDAMENTAL RULES - FULL AUTO =====
F = {
    'min_market_cap_cr': 2000,
    'max_debt_equity': 0.5,
    'max_beta': 0.9,
    'min_roe': 18.0, # Auto calculated
    'min_roce': 18.0, # Auto calculated
    'min_eps_cagr_5y': 12.0,
    'max_pe': 60,
}

# ===== 3. TECHNICAL RULES =====
R = {
    'base_min_days': 30, 'base_max_days': 60, 'base_range_max_pct': 15.0,
    'base_vol_dry_pct': 0.40, 'bo_vol_spike': 2.5, 'bo_buffer_pct': 0.5,
    'retest_vol_max_pct': 0.30, 'retest_zone_pct': 3.0,
    'sl_buffer_pct': 1.0, 'target_r': 2.0,
    'min_price': 50, 'min_daily_value_cr': 0.5,
    'max_risk_pct': 15.0, 'min_rr_pct': 8.0,
}

def get_fundamentals_auto(stock):
    """PURA FUNDAMENTAL AUTO - yfinance se sab nikal"""
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info

        # 1. Basic Info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['beta'] = round(info.get('beta', 99), 2)
        fund_data['pe'] = round(info.get('trailingPE', 99), 1)

        if fund_data['market_cap_cr'] < F['min_market_cap_cr']: return False, fund_data
        if fund_data['beta'] > F['max_beta']: return False, fund_data
        if fund_data['pe'] > F['max_pe']: return False, fund_data

        # 2. Balance Sheet - Debt/Equity
        bs = t.balance_sheet
        if not bs.empty:
            debt = bs.loc['Total Debt'].iloc[0] if 'Total Debt' in bs.index else 0
            equity = bs.loc['Total Stockholder Equity'].iloc[0] if 'Total Stockholder Equity' in bs.index else 1
            fund_data['debt_equity'] = round(debt / equity, 2) if equity else 99
            if fund_data['debt_equity'] > F['max_debt_equity']: return False, fund_data

        # 3. Financials - ROE, ROCE, EPS CAGR
        fin = t.financials
        if not fin.empty and len(fin.columns) >= 3:
            net_income = fin.loc['Net Income'] if 'Net Income' in fin.index else pd.Series()
            ebit = fin.loc['EBIT'] if 'EBIT' in fin.index else pd.Series()

            # ROE = Net Income / Avg Equity
            if not net_income.empty and not bs.empty:
                avg_equity = bs.loc['Total Stockholder Equity'].iloc[:2].mean() if 'Total Stockholder Equity' in bs.index else 1
                fund_data['roe'] = round(net_income.iloc[0] / avg_equity * 100, 1) if avg_equity else 0
                if fund_data['roe'] < F['min_roe']: return False, fund_data

            # ROCE = EBIT / Capital Employed
            if not ebit.empty and not bs.empty:
                total_assets = bs.loc['Total Assets'].iloc[0] if 'Total Assets' in bs.index else 0
                curr_liab = bs.loc['Current Liabilities'].iloc[0] if 'Current Liabilities' in bs.index else 0
                capital_employed = total_assets - curr_liab
                fund_data['roce'] = round(ebit.iloc[0] / capital_employed * 100, 1) if capital_employed else 0
                if fund_data['roce'] < F['min_roce']: return False, fund_data

            # EPS CAGR 5Y
            if len(net_income) >= 4:
                oldest = net_income.iloc[-1]
                latest = net_income.iloc[0]
                years = len(net_income) - 1
                if oldest > 0 and years > 0:
                    fund_data['eps_cagr_5y'] = round(((latest / oldest) ** (1/years) - 1) * 100, 1)
                    if fund_data['eps_cagr_5y'] < F['min_eps_cagr_5y']: return False, fund_data

        fund_data['fund_pass'] = True
        return True, fund_data
    except:
        return False, fund_data

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

def find_base_and_breakout(df, idx):
    if idx < 100: return False, {}
    row = df.iloc[idx]
    if row['50DMA'] < row['200DMA'] or row['Close'] < row['50DMA']: return False, {}

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
        }
    return False, {}

def check_retest_entry(df, idx, bo_data):
    if idx <= bo_data['bo_idx'] + 1: return False, {}
    row = df.iloc[idx]
    bo_level = bo_data['bo_level']
    bo_vol = bo_data['bo_vol']

    zone_low = bo_level * (1 - R['retest_zone_pct']/100)
    zone_high = bo_level * (1 + R['retest_zone_pct']/100)
    if not (zone_low <= row['Low'] <= zone_high): return False, {}
    if row['Volume'] > bo_vol * R['retest_vol_max_pct']: return False, {}
    if row['Close'] < row['Open'] * 0.995: return False, {}

    swing_low = df['Low'].iloc[idx-5:idx+1].min()
    sl_price = swing_low * (1 - R['sl_buffer_pct']/100)
    risk = row['Close'] - sl_price
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['max_risk_pct'] or risk_pct <= 0: return False, {}

    target = row['Close'] + risk * R['target_r']
    target_pct = (target - row['Close']) / row['Close'] * 100
    if target_pct < R['min_rr_pct']: return False, {}

    return True, {
        'Entry_Date': df.index[idx].strftime('%Y-%m-%d'),
        'Entry': round(row['Close'], 2), 'SL': round(sl_price, 2),
        'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
        'Reward_%': round(target_pct, 1), 'RR': R['target_r'],
        'BO_Level': bo_level, 'BO_Date': bo_data['bo_date']
    }

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def scan_stock_full_auto(stock):
    # STEP 1: FUNDAMENTAL AUTO FILTER
    fund_pass, fund_data = get_fundamentals_auto(stock)
    if not fund_pass: return [], fund_data

    # STEP 2: TECHNICAL SCAN
    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=300),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=15)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200: return [], fund_data

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100: return [], fund_data
        df = add_indicators(df)
        results = []
        last_bo_idx = -100

        for i in range(100, len(df)):
            if not check_liquidity(df, i): continue
            is_bo, bo_data = find_base_and_breakout(df, i)
            if is_bo and i > last_bo_idx + 20:
                last_bo_idx = i
                for j in range(i+1, min(i+21, len(df))):
                    is_entry, entry_data = check_retest_entry(df, j, bo_data)
                    if is_entry:
                        result, exit_date, pnl = simulate_trade(df, j, entry_data['SL'], entry_data['Target'])
                        entry_data.update({
                            'Stock': stock, 'Result': result,
                            'Exit_Date': exit_date, 'PnL_%': pnl,
                            **fund_data
                        })
                        results.append(entry_data)
                        break
        return results, fund_data
    except: return [], fund_data

# ===== MAIN BACKTEST =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks - FULL AUTO MODE...", flush=True)

all_results = []
fund_passed = []
for i, stock in enumerate(stocks):
    trades, fund_data = scan_stock_full_auto(stock)
    if fund_data and fund_data.get('fund_pass'): fund_passed.append({'Stock': stock, **fund_data})
    all_results.extend(trades)
    if i % 10 == 0: print(f"Done {i}/{len(stocks)} | Fund Pass: {len(fund_passed)} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(fund_passed)

if not all_results:
    print("0 SETUP MILA - Quality + Technical dono match nahi hua")
    exit()

df_res = pd.DataFrame(all_results).sort_values('Entry_Date')

total = len(df_res)
wins = len(df_res[df_res['Result'] == 'WIN'])
winrate = round(wins / total * 100, 1) if total else 0
total_pnl = df_res['PnL_%'].sum()

summary = pd.DataFrame([{
    'Total_Stocks_Scanned': len(stocks),
    'Fundamental_Pass': len(fund_passed),
    'Total_Setups': total, 'Wins': wins, 'Winrate_%': winrate,
    'Total_PnL_%': round(total_pnl, 1),
    'Strategy': 'VA-PA Q-FACTOR V4 FULL AUTO'
}])

def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=10000, cols=50)
        ws.clear()
    if not df.empty:
        payload = [df.columns.values.tolist()] + df.fillna('').values.tolist()
        ws.update('A1', payload)
        return len(df)
    return 0

update_gsheet('QFACTOR_V4_TRADES', df_res)
update_gsheet('QFACTOR_V4_SUMMARY', summary)
update_gsheet('QFACTOR_V4_FUND_PASS', df_fund)

print(f"\n=== FULL AUTO COMPLETE ===", flush=True)
print(f"Scanned: {len(stocks)} | Fund Pass: {len(fund_passed)} | Setups: {total}", flush=True)
print(f"Winrate: {winrate}% | Net P&L: {total_pnl}%", flush=True)
