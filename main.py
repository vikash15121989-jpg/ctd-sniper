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

print("=== VA-PA Q-FACTOR V8.3 FINAL - PRODUCTION READY ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30) # LOCKED

F = {'min_market_cap_cr': 500, 'max_debt_equity': 10.0, 'max_pe': 1000}
R = {'min_price': 50, 'min_daily_value_cr': 0.5, 'sl_buffer_pct': 3.0, 'target_r': 1.0, 'max_risk_pct': 25.0, 'vol_blast_ratio': 1.2, 'rs_days': 30}

debug_fund = []
all_results = []

# Nifty data
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.droplevel(1)
nifty = nifty[nifty.index <= BACKTEST_END]
nifty['52W_High'] = nifty['High'].rolling(252, min_periods=252).max()

def get_fundamentals_v8_3(stock):
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

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl: return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target: return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def check_rs_breakout_v8_3(df, idx):
    if idx < 252: return False, {}, "Data < 252"
    row = df.iloc[idx]
    entry_date = df.index[idx]

    if row['Close'] < R['min_price']: return False, {}, "Price < 50"
    avg_value_cr = (df['Close'].iloc[idx-20:idx] * df['Volume'].iloc[idx-20:idx]).mean() / 1e7
    if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']: return False, {}, "Liquidity < 0.5Cr"

    high_252 = df['High'].iloc[idx-252:idx].max()
    if pd.isna(high_252) or row['Close'] <= high_252 * 1.01: return False, {}, "No 52W BO"

    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0 or pd.isna(avg_vol_20): avg_vol_20 = 1
    vol_ratio = row['Volume'] / avg_vol_20
    if vol_ratio < R['vol_blast_ratio']: return False, {}, f"Vol {vol_ratio:.1f}x < 1.2x"

    rs_days_diff = 999
    try:
        nifty_52h_date = nifty['52W_High'].iloc[:nifty.index.get_loc(entry_date)+1].idxmax()
        if pd.notna(nifty_52h_date):
            rs_days_diff = abs((entry_date - nifty_52h_date).days)
            if rs_days_diff > R['rs_days']: return False, {}, f"RS {rs_days_diff}d > 30d"
    except:
        return False, {}, "Nifty data error"

    return True, {
        'bo_date': entry_date.strftime('%Y-%m-%d'), 'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2), 'vol_blast_x': round(vol_ratio, 1),
        'rs_days_diff': rs_days_diff, 'liquidity_cr': round(avg_value_cr, 2)
    }, "V8.3 PASS"

def scan_stock_v8_3(stock):
    global debug_fund
    fund_pass, fund_data, fund_reason = get_fundamentals_v8_3(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass: return []

    try:
        df = yf.download(f"{stock}.NS
