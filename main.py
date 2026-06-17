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
BACKTEST_END = datetime(2026, 5, 30)

# ===== 2. FUNDAMENTAL FILTERS =====
F = {
    'min_market_cap_cr': 500,
    'max_debt_equity': 10.0,
    'max_pe': 1000
}

# ===== 3. TECHNICAL FILTERS - V8.3 =====
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'sl_buffer_pct': 3.0,
    'target_r': 1.0,
    'max_risk_pct': 25.0,
    'vol_blast_ratio': 1.2,
    'rs_days': 30
}

debug_fund = []
all_results = []

# ===== NIFTY DATA =====
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)
nifty = nifty[nifty.index <= BACKTEST_END]
nifty['52W_High'] = nifty['High'].rolling(252, min_periods=252).max()

print(f"Nifty data loaded: {len(nifty)} days", flush=True)

def get_fundamentals_v8_3(stock):
    fund_data = {'stock': stock}
    try:
        t = yf.Ticker(f"{stock}.NS")
        info = t.info
        fund_data['market_cap_cr'] = round(info.get('marketCap', 0) / 1e7, 0)
        fund_data['debt_equity'] = info.get('debtToEquity', 999)
        fund_data['pe'] = info.get('trailingPE', 999)
        if fund_data['market_cap_cr'] < F['min_market_cap_cr']:
            return False, fund_data, "Mcap fail"
        if fund_data['debt_equity'] > F['max_debt_equity']:
            return False, fund_data, "DE fail"
        if fund_data['pe'] > F['max_pe']:
            return False, fund_data, "PE fail"
        return True, fund_data, "PASS"
    except Exception as e:
        return False, fund_data, f"Error: {str(e)[:30]}"

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def check_rs_breakout_v8_3(df, idx):
    if idx < 252:
        return False, {}, "Data < 252"

    row = df.iloc[idx]
    entry_date = df.index[idx]

    if row['Close'] < R['min_price']:
        return False, {}, "Price < 50"

    avg_value_cr = (df['Close'].iloc[idx-20:idx] * df['Volume'].iloc[idx-20:idx]).mean() / 1e7
    if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
        return False, {}, "Liquidity < 0.5Cr"

    high_252 = df['High'].iloc[idx-252:idx].max()
    if pd.isna(high_252) or row['Close'] <= high_252 * 1.01:
        return False, {}, "No 52W BO"

    avg_vol_20 = df['Volume'].iloc[idx-20:idx].mean()
    if avg_vol_20 == 0 or pd.isna(avg_vol_20):
        avg_vol_20 = 1
    vol_ratio = row['Volume'] / avg_vol_20
    if vol_ratio < R['vol_blast_ratio']:
        return False, {}, f"Vol {vol_ratio:.1f}x < 1.2x"

    rs_days_diff = 999
    try:
        nifty_52h_date = nifty['52W_High'].iloc[:nifty.index.get_loc(entry_date)+1].idxmax()
        if pd.notna(nifty_52h_date):
            rs_days_diff = abs((entry_date - nifty_52h_date).days)
            if rs_days_diff > R['rs_days']:
                return False, {}, f"RS {rs_days_diff}d > 30d"
    except:
        return False, {}, "Nifty data error"

    return True, {
        'bo_date': entry_date.strftime('%Y-%m-%d'),
        'bo_level': round(high_252, 2),
        '52w_high': round(high_252, 2),
        'vol_blast_x': round(vol_ratio, 1),
        'rs_days_diff': rs_days_diff,
        'liquidity_cr': round(avg_value_cr, 2)
    }, "V8.3 PASS"

def scan_stock_v8_3(stock):
    global debug_fund
    fund_pass, fund_data, fund_reason = get_fundamentals_v8_3(stock)
    debug_fund.append({'Stock': stock, 'Pass': fund_pass, 'Reason': fund_reason, **fund_data})
    if not fund_pass:
        return []

    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[df.index <= BACKTEST_END].dropna()
        if len(df) < 300:
            return []
        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100:
            return []

        results = []
        last_entry_idx = -100

        for i in range(252, len(df)):
            if i - last_entry_idx < 100:
                continue

            is_bo, bo_data, bo_reason = check_rs_breakout_v8_3(df, i)
            if is_bo:
                entry_price = df['Close'].iloc[i]
                sl_price = df['Low'].iloc[i-20:i+1].min() * (1 - R['sl_buffer_pct']/100)
                risk = entry_price - sl_price
                risk_pct = risk / entry_price * 100
                if risk_pct > R['max_risk_pct'] or risk_pct <= 0:
                    continue

                target = entry_price + risk * R['target_r']
                result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

                results.append({
                    'Stock': stock,
                    'Entry_Date': df.index[i].strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2),
                    'SL': round(sl_price, 2),
                    'Target': round(target, 2),
                    'Risk_%': round(risk_pct, 1),
                    'Result': result,
                    'Exit_Date': exit_date,
                    'PnL_%': pnl,
                    **bo_data,
                    **fund_data
                })
                last_entry_idx = i
        return results
    except Exception as e:
        return []

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))
print(f"Loaded {len(stocks)} stocks - Sorted", flush=True)

print(f"Scanning {len(stocks)} stocks - V8.3 FINAL...", flush=True)
for i, stock in enumerate(stocks):
    trades = scan_stock_v8_3(stock)
    all_results.extend(trades)
    if i % 50 == 0:
        print(f"Done {i+1}/{len(stocks)} | Setups: {len(all_results)}", flush=True)

# ===== SUMMARY =====
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
    drawdown_pct = ((equity_curve - running_max) / (100 + running_max) * 100)
    max_drawdown = round(drawdown_pct.min(), 1) if len(drawdown_pct) else 0
else:
    total = wins = winrate = total_pnl = avg_win = avg_loss = max_drawdown = 0
    df_res = pd.DataFrame()

print(f"\n=== V8.3 FINAL COMPLETE ===", flush=True)
print(f"Total Setups: {total} | Winrate: {winrate}% | Total PnL: {total_pnl:.1f}%", flush=True)
print(f"Avg Win: {avg_win:.1f}% | Avg Loss: {avg_loss:.1f}% | Max DD: {max_drawdown}%", flush=True)

# ===== GSHEET UPDATE =====
try:
    summary = pd.DataFrame([{
        'Total_Stocks': len(stocks),
        'Total_Setups': total,
        'Winrate_%': winrate,
        'Avg_Win_%': round(avg_win, 1),
        'Avg_Loss_%': round(avg_loss, 1),
        'Total_PnL_%': round(total_pnl, 1),
        'Max_Drawdown_%': max_drawdown,
        'Strategy': 'V8.3 FINAL'
    }])

    df_res = df_res.replace([np.inf, -np.inf], np.nan).fillna(0)
    df_fund = df_fund.replace([np.inf, -np.inf], np.nan).fillna(0)
    summary = summary.replace([np.inf, -np.inf], np.nan).fillna(0)

    def get_or_create_ws(sh, title):
        try:
            return sh.worksheet(title)
        except:
            return sh.add_worksheet(title=title, rows=5000, cols=30)

    ws_trades = get_or_create_ws(sh, "QFACTOR_V8_3_FINAL_TRADES")
    ws_trades.clear()
    if not df_res.empty:
        ws_trades.update([df_res.columns.values.tolist()] + df_res.values.tolist())

    ws_summary = get_or_create_ws(sh, "SUMMARY_V8_3_FINAL")
    ws_summary.clear()
    ws_summary.update([summary.columns.values.tolist()] + summary.values.tolist())

    ws_fund = get_or_create_ws(sh, "FUNDAMENTAL_DEBUG_V8_3")
    ws_fund.clear()
    ws_fund.update([df_fund.columns.values.tolist()] + df_fund.values.tolist())

    print("GSheet updated successfully", flush=True)
except Exception as e:
    print(f"GSheet update failed: {str(e)[:100]}", flush=True)
