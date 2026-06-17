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

print("=== VA-PA Q-FACTOR V8.5 COMBO - FIRST BO + RE-BO ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL =====
F = {'min_market_cap_cr': 500, 'max_debt_equity': 10.0, 'max_pe': 1000}

# ===== 3. TECHNICAL - V8.5 COMBO =====
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'sl_buffer_pct': 3.0,
    'target_r': 1.0,
    'max_risk_pct': 25.0, # 25% safe
    'vol_blast_ratio': 1.2,
    'rs_days': 30,
    'lookback_rs_days': 90, # Re-BO ke liye
}

debug_fund = []
debug_tech = []

# Nifty data
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)

nifty['52W_High'] = nifty['High'].rolling(252).max()
nifty['52W_High_Date'] = pd.NaT
for i in range(252, len(nifty)):
    window = nifty['High'].iloc[i-252:i]
    max_idx = window.idxmax()
    nifty.loc[nifty.index[i], '52W_High_Date'] = max_idx

def get_fundamentals_v8_5(stock):
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

def check_combo_breakout_v8_5(df, idx):
    if idx < 252: return False, {}, "Data < 252", None

    row = df.iloc[idx]
    entry_date = df.index[idx]

    # Basic filters
    if row['Close'] < R['min_price']: return False, {}, "Price < 50", None
    avg_value_cr = (df['Close'].iloc[idx-20:idx] * df['Volume'].iloc[idx-20:idx]).mean() / 1e7
    if avg_value_cr < R['min_daily_value_cr']: return False, {}, "Liquidity < 0.5Cr", None

    # Aaj 52W BO hona chahiye
    high_252 = df['High'].iloc[idx-252:idx].max()
    if row['Close'] <= high_252 * 1.01: return False, {}, "No 52W BO today", None

    # Volume blast
    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0: avg_vol_20 = 1
    vol_ratio = row['Volume'] / avg_vol_20
    if vol_ratio < R['vol_blast_ratio']: return False, {}, f"Vol {vol_ratio:.1f}x < 1.2x", None

    # === CONDITION A: FIRST TIME RS BO ===
    rs_first_bo = False
    rs_days_diff = 999
    try:
        nifty_52h_date = nifty['52W_High_Date'].loc[entry_date]
        if pd.notna(nifty_52h_date):
            rs_days_diff = (entry_date - nifty_52h_date).days
            if abs(rs_days_diff) <= R['rs_days']:
                rs_first_bo = True
    except: pass

    # === CONDITION B: RE-BREAKOUT AFTER RS PROVEN ===
    rs_proven_rebo = False
    first_bo_date = None
    if not rs_first_bo: # Agar first BO nahi hai to Re-BO check karo
        for i in range(idx - R['lookback_rs_days'], idx):
            if i < 252: continue
            past_high_252 = df['High'].iloc[i-252:i].max()
            # Past me BO hua tha
            if df['Close'].iloc[i] > past_high_252 * 1.01 and df['Close'].iloc[i-1] <= past_high_252 * 1.01:
                try:
                    nifty_52h_date = nifty['52W_High_Date'].loc[df.index[i]]
                    if pd.notna(nifty_52h_date):
                        days_diff = (df.index[i] - nifty_52h_date).days
                        if abs(days_diff) <= R['rs_days']:
                            rs_proven_rebo = True
                            first_bo_date = df.index[i]
                            break
                except: continue

    # Dono me se koi ek TRUE hona chahiye
    if not rs_first_bo and not rs_proven_rebo:
        return False, {}, "No RS: Neither First BO nor Re-BO", None

    entry_type = "FIRST_BO" if rs_first_bo else "RE_BO"

    return True, {
        'bo_date': entry_date.strftime('%Y-%m-%d'),
        'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2),
        'vol_blast_x': round(vol_ratio, 1),
        'entry_type': entry_type,
        'rs_days_diff': rs_days_diff if rs_first_bo else 0,
        'first_rs_bo_date': first_bo_date.strftime('%Y-%m-%d') if first_bo_date else '',
        'liquidity_cr': round(avg_value_cr, 2)
    }, f"V8.5 COMBO PASS - {entry_type}", entry_type

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl: return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target: return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def scan_stock_v8_5(stock):
    global debug_fund, debug_tech
    fund_pass, fund_data, fund_reason = get_fundamentals_v8_5(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) < 300: return []
        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100: return []

        results = []
        last_entry_idx = -100 # Ek stock me multiple entry ke liye, par 100 din ka gap

        for i in range(252, len(df)):
            if i - last_entry_idx < 100: continue # 100 din ka cooldown

            is_bo, bo_data, bo_reason, entry_type = check_combo_breakout_v8_5(df, i)
            if is_bo:
                entry_price = df['Close'].iloc[i]
                sl_price = df['Low'].iloc[i-20:i+1].min() * (1 - R['sl_buffer_pct']/100)
                risk = entry_price - sl_price
                risk_pct = risk / entry_price * 100
                if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue

                target = entry_price + risk * R['target_r']
                result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

                entry_data = {
                    'Stock': stock, 'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry_Type': entry_type, 'Entry': round(entry_price, 2),
                    'SL': round(sl_price, 2), 'Target': round(target, 2),
                    'Risk_%': round(risk_pct, 1), 'Result': result,
                    'Exit_Date': exit_date, 'PnL_%': pnl, **bo_data, **fund_data
                }
                results.append(entry_data)
                last_entry_idx = i # Is stock me agli entry 100 din baad

        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:40]}'})
        return []

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

print(f"Scanning {len(stocks)} stocks - V8.5 COMBO MODE...", flush=True)
all_results = []
for i, stock in enumerate(stocks):
    trades = scan_stock_v8_5(stock)
    all_results.extend(trades)
    if i % 50 == 0: print(f"Done {i+1}/{len(stocks)} | Setups: {len(all_results)}", flush=True)

df_fund = pd.DataFrame(debug_fund)
if all_results:
    df_res = pd.DataFrame(all_results).sort_values('Entry_Date')
    total = len(df_res)
    wins = len(df_res[df_res['Result'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_res['PnL_%'].sum()
    avg_win = df_res[df_res['Result'] == 'WIN']['PnL_%'].mean() if wins > 0 else 0
    avg_loss = df_res[df_res['Result'] == 'LOSS']['PnL_%'].mean() if len(df_res[df_res['Result'] == 'LOSS']) > 0 else 0
    equity_curve = df_res['PnL_%'].cumsum()
    running_max = equity_curve.cummax()
    drawdown = equity_curve - running_max
    max_drawdown = round(drawdown.min(), 1)

    first_bo_count = len(df_res[df_res['Entry_Type'] == 'FIRST_BO'])
    rebo_count = len(df_res[df_res['Entry_Type'] == 'RE_BO'])
else:
    total = wins = winrate = total_pnl = avg_win = avg_loss = max_drawdown = 0
    first_bo_count = rebo_count = 0
    df_res = pd.DataFrame()

summary = pd.DataFrame([{
    'Total_Stocks': len(stocks),
    'Total_Setups': total,
    'First_BO': first_bo_count,
    'Re_BO': rebo_count,
    'Winrate_%': winrate,
    'Avg_Win_%': round(avg_win, 1) if wins > 0 else 0,
    'Avg_Loss_%': round(avg_loss, 1) if total > wins else 0,
    'Total_PnL_%': round(total_pnl, 1),
    'Max_Drawdown_%': round(max_drawdown, 1),
    'Strategy': 'V8.5 COMBO'
}])

# GSheet update code same as V8.3...
print(f"\n=== V8.5 COMBO COMPLETE ===", flush=True)
print(f"Total Setups: {total} | First BO: {first_bo_count} | Re-BO: {rebo_count}", flush=True)
print(f"Winrate: {winrate}% | Total PnL: {total_pnl:.1f}% | Max DD: {max_drawdown}%", flush=True)
