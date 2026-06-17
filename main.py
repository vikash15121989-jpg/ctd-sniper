import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V6 - VOLUME COMPRESSION BREAKOUT ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. RELAXED FUNDAMENTAL RULES =====
F = {
    'min_market_cap_cr': 500, # Aur relax kiya
    'max_debt_equity': 1.5,
    'max_beta': 1.5,
    'min_roe': 8.0,
    'min_roce': 8.0,
    'min_eps_cagr_5y': 3.0,
    'max_pe': 100,
}

# ===== 3. TECHNICAL RULES - COMPRESSION =====
R = {
    'base_min_days': 15, 'base_max_days': 120, 'base_range_max_pct': 35.0,
    'base_vol_dry_pct': 0.80, # Base me volume 80% tak dry chalta hai
    'bo_vol_spike': 1.5, # 1.5x bhi chalta hai ab
    'bo_buffer_pct': 0.5,
    'retest_vol_max_pct': 0.70, 'retest_zone_pct': 7.0,
    'sl_buffer_pct': 1.5, 'target_r': 1.5,
    'min_price': 20, 'min_daily_value_cr': 0.1,
    'max_risk_pct': 25.0, 'min_rr_pct': 4.0,
    # NAYA: COMPRESSION RULES
    'compression_days': 10, # 10 din ka high check
    'vol_dry_ratio': 0.5, # Volume aadha ho jana chahiye
    'vol_blast_ratio': 1.5, # Phir 1.5x blast
}

debug_fund = []
debug_tech = []

def get_fundamentals_debug(stock):
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

        bs = t.balance_sheet
        if not bs.empty:
            debt = bs.loc['Total Debt'].iloc[0] if 'Total Debt' in bs.index else 0
            equity = bs.loc['Total Stockholder Equity'].iloc[0] if 'Total Stockholder Equity' in bs.index else 1
            fund_data['debt_equity'] = round(debt / equity, 2) if equity else 99

        fin = t.financials
        if not fin.empty and len(fin.columns) >= 2:
            net_income = fin.loc['Net Income'] if 'Net Income' in fin.index else pd.Series()
            ebit = fin.loc['EBIT'] if 'EBIT' in fin.index else pd.Series()

            if not net_income.empty and not bs.empty:
                avg_equity = bs.loc['Total Stockholder Equity'].iloc[:2].mean() if 'Total Stockholder Equity' in bs.index else 1
                fund_data['roe'] = round(net_income.iloc[0] / avg_equity * 100, 1) if avg_equity else 0

            if not ebit.empty and not bs.empty:
                total_assets = bs.loc['Total Assets'].iloc[0] if 'Total Assets' in bs.index else 0
                curr_liab = bs.loc['Current Liabilities'].iloc[0] if 'Current Liabilities' in bs.index else 0
                capital_employed = total_assets - curr_liab
                fund_data['roce'] = round(ebit.iloc[0] / capital_employed * 100, 1) if capital_employed else 0

            if len(net_income) >= 3:
                oldest = net_income.iloc[-1]
                latest = net_income.iloc[0]
                years = len(net_income) - 1
                if oldest > 0 and years > 0:
                    fund_data['eps_cagr_5y'] = round(((latest / oldest) ** (1/years) - 1) * 100, 1)

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
    df['High_10D'] = df['High'].rolling(10).max() # 10 din ka high
    return df

def check_liquidity(df, idx):
    try:
        if df['Close'].iloc[idx] < R['min_price']: return False
        if df['Daily_Value_20MA'].iloc[idx] < R['min_daily_value_cr'] * 1e7: return False
        return True
    except: return False

def check_volume_compression(df, idx):
    """
    NAYA RULE: 10 din ka high nahi toota + Volume dry hoke blast
    """
    if idx < R['compression_days'] + 5: return False, {}

    # 1. 10 din ka high nikalo
    high_10d_idx = df['High'].iloc[idx-R['compression_days']:idx].idxmax()
    high_10d_date = high_10d_idx
    high_10d_price = df['High'].loc[high_10d_date]
    high_10d_vol = df['Volume'].loc[high_10d_date]

    # 2. Kya us high ke baad koi naya high bana? Nahi banana chahiye
    if df['High'].iloc[df.index.get_loc(high_10d_date)+1:idx+1].max() >= high_10d_price:
        return False, {} # High tut gaya

    # 3. Volume dry hua kya? High wale din se aaj tak
    days_since_high = idx - df.index.get_loc(high_10d_date)
    if days_since_high < 3: return False, {} # Kam se kam 3 din compression

    avg_vol_since_high = df['Volume'].iloc[df.index.get_loc(high_10d_date):idx].mean()
    if avg_vol_since_high > high_10d_vol * R['vol_dry_ratio']:
        return False, {} # Volume aadha nahi hua

    # 4. Aaj volume blast hua kya?
    today_vol = df['Volume'].iloc[idx]
    if today_vol < avg_vol_since_high * R['vol_blast_ratio']:
        return False, {} # Volume blast nahi

    return True, {
        'high_10d_date': high_10d_date.strftime('%Y-%m-%d'),
        'high_10d_price': round(high_10d_price, 2),
        'high_10d_vol': int(high_10d_vol),
        'avg_vol_dry': int(avg_vol_since_high),
        'today_vol': int(today_vol),
        'compression_days': days_since_high
    }

def find_base_and_breakout_v6(df, idx, stock):
    row = df.iloc[idx]
    tech_reason = ""

    if row['50DMA'] < row['200DMA']:
        return False, {}, "50DMA < 200DMA"
    if row['Close'] < row['50DMA']:
        return False, {}, "Close < 50DMA"

    # NAYA: VOLUME COMPRESSION CHECK
    is_comp, comp_data = check_volume_compression(df, idx)
    if not is_comp:
        return False, {}, "No Volume Compression"

    # Compression ke saath base bhi chahiye
    for base_days in range(R['base_min_days'], R['base_max_days'] + 1):
        if idx - base_days < 0: continue
        base_df = df.iloc[idx-base_days:idx]
        base_high = base_df['High'].max()
        base_low = base_df['Low'].min()
        base_range = (base_high - base_low) / base_low * 100
        if base_range > R['base_range_max_pct']: continue

        bo_level = base_high * (1 + R['bo_buffer_pct']/100)
        if row['Close'] <= bo_level: continue
        if row['Close'] <= row['Open']: continue

        return True, {
            'bo_date': df.index[idx].strftime('%Y-%m-%d'),
            'bo_level': round(base_high, 2),
            'bo_vol': row['Volume'],
            'bo_idx': idx,
            **comp_data # Compression data bhi add
        }, "BO + COMPRESSION PASS"

    return False, {}, "Base Fail"

def check_retest_entry(df, idx, bo_data):
    if idx <= bo_data['bo_idx'] + 1: return False, {}, "No data after BO"
    row = df.iloc[idx]
    bo_level = bo_data['bo_level']

    zone_low = bo_level * (1 - R['retest_zone_pct']/100)
    zone_high = bo_level * (1 + R['retest_zone_pct']/100)
    if not (zone_low <= row['Low'] <= zone_high): return False, {}, "Not in Retest Zone"

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

def scan_stock_v6(stock):
    global debug_fund, debug_tech

    fund_pass, fund_data, fund_reason = get_fundamentals_debug(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

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

        for i in range(100, len(df)):
            if not check_liquidity(df, i): continue
            is_bo, bo_data, bo_reason = find_base_and_breakout_v6(df, i, stock)
            if is_bo and i > last_bo_idx + 20:
                last_bo_idx = i
                # Compression BO me direct entry bhi le sakte, retest optional
                entry_data = {
                    'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry': round(df['Close'].iloc[i], 2),
                    'SL': round(bo_data['bo_level'] * 0.98, 2), # 2% below BO
                    'Target': round(df['Close'].iloc[i] * 1.15, 2), # 15% target
                    'BO_Level': bo_data['bo_level'],
                    'BO_Date': bo_data['bo_date'],
                    'Compression_Days': bo_data['compression_days'],
                    'High_10D_Date': bo_data['high_10d_date'],
                    'Vol_Blast_X': round(df['Volume'].iloc[i] / bo_data['avg_vol_dry'], 1)
                }
                result, exit_date, pnl = simulate_trade(df, i, entry_data['SL'], entry_data['Target'])
                entry_data.update({
                    'Stock': stock, 'Result': result,
                    'Exit_Date': exit_date, 'PnL_%': pnl,
                    **fund_data, **bo_data
                })
                results.append(entry_data)
                debug_tech.append({'Stock': stock, 'Reason': 'COMPRESSION BO SETUP'})
                break

        if not results:
            debug_tech.append({'Stock': stock, 'Reason': 'No Compression BO'})

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:50]}'})
        return []

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks - COMPRESSION MODE...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v6(stock)
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
    'Strategy': 'V6 COMPRESSION BREAKOUT'
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
    update_gsheet('QFACTOR_V6_TRADES', df_res)
update_gsheet('QFACTOR_V6_SUMMARY', summary)

print(f"\n=== COMPLETE ===", flush=True)
print(f"Check QFACTOR_V6_TRADES sheet - Compression_BO setup milega", flush=True)
