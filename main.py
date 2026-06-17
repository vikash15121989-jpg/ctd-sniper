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

print("=== VA-PA Q-FACTOR V8.6 FIXED - RS WINDOW ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

F = {'min_market_cap_cr': 500, 'max_debt_equity': 10.0, 'max_pe': 1000}
R = {'min_price': 50, 'min_daily_value_cr': 0.5, 'sl_buffer_pct': 3.0, 'target_r': 1.0, 'max_risk_pct': 25.0, 'vol_blast_ratio': 1.2, 'rs_days': 30}

debug_fund = []
debug_tech = []
all_results = []

# Nifty data
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.droplevel(1)
nifty['52W_High'] = nifty['High'].rolling(252).max()

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

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl: return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target: return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

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
        rs_valid_start = None
        rs_valid_end = None
        last_entry_idx = -100

        for i in range(252, len(df)):
            if i - last_entry_idx < 60: continue
            row = df.iloc[i]
            entry_date = df.index[i]

            if row['Close'] < R['min_price']: continue
            avg_value_cr = (df['Close'].iloc[i-20:i] * df['Volume'].iloc[i-20:i]).mean() / 1e7
            if avg_value_cr < R['min_daily_value_cr']: continue

            high_252 = df['High'].iloc[i-252:i].max()
            is_bo_today = row['Close'] > high_252 * 1.01 and df['Close'].iloc[i-1] <= high_252 * 1.01
            if not is_bo_today: continue

            avg_vol_20 = df['Volume'].iloc[i-20:i].mean()
            if avg_vol_20 == 0: avg_vol_20 = 1
            vol_ratio = row['Volume'] / avg_vol_20
            if vol_ratio < R['vol_blast_ratio']: continue

            entry_type = None
            for nifty_date in reversed(nifty_52h_dates):
                if nifty_date <= entry_date:
                    days_diff = (entry_date - nifty_date).days
                    if abs(days_diff) <= R['rs_days']:
                        entry_type = "FIRST_RS_BO"
                        rs_valid_start = entry_date
                        next_highs = [d for d in nifty_52h_dates if d > nifty_date]
                        rs_valid_end = next_highs[0] if next_highs else BACKTEST_END
                        break
                    else: break

            if not entry_type and rs_valid_start and entry_date >= rs_valid_start and entry_date < rs_valid_end:
                entry_type = "RS_WINDOW_BO"

            if not entry_type: continue

            entry_price = row['Close']
            sl_price = df['Low'].iloc[i-20:i+1].min() * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue

            target = entry_price + risk * R['target_r']
            result, exit_date, pnl = simulate_trade(df, i, sl_price, target)

            results.append({
                'Stock': stock, 'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                'Entry_Type': entry_type, 'Entry': round(entry_price, 2),
                'SL': round(sl_price, 2), 'Target': round(target, 2),
                'Risk_%': round(risk_pct, 1), 'Result': result,
                'Exit_Date': exit_date, 'PnL_%': pnl,
                'RS_Valid_From': rs_valid_start.strftime('%Y-%m-%d') if rs_valid_start else '',
                'RS_Valid_Till': rs_valid_end.strftime('%Y-%m-%d') if rs_valid_end else '',
                '52W_High': round(high_252, 2), 'Vol_X': round(vol_ratio, 1),
                **fund_data
            })
            last_entry_idx = i
        return results
    except Exception as e:
        debug_tech.append({'Stock': stock, 'Reason': f'Error: {str(e)[:40]}'})
        return []

# ===== MAIN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Loaded {len(stocks)} stocks", flush=True)

print(f"Scanning {len(stocks)} stocks - V8.6 RS WINDOW MODE...", flush=True)
for i, stock in enumerate(stocks):
    trades = scan_stock_v8_6(stock)
    all_results.extend(trades)
    if i % 50 == 0: print(f"Done {i+1}/{len(stocks)} | Setups: {len(all_results)}", flush=True)

# ===== SUMMARY CALCULATE =====
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
    max_drawdown = round((equity_curve - equity_curve.cummax()).min(), 1)
    first_bo_count = len(df_res[df_res['Entry_Type'] == 'FIRST_RS_BO'])
    rebo_count = len(df_res[df_res['Entry_Type'] == 'RS_WINDOW_BO'])
else:
    total = wins = winrate = total_pnl = avg_win = avg_loss = max_drawdown = 0
    first_bo_count = rebo_count = 0
    df_res = pd.DataFrame()

print(f"\n=== V8.6 RS WINDOW COMPLETE ===", flush=True)
print(f"Total Setups: {total} | First BO: {first_bo_count} | Re-BO: {rebo_count}", flush=True)
print(f"Winrate: {winrate}% | Total PnL: {total_pnl:.1f}% | Max DD: {max_drawdown}%", flush=True)
print(f"Avg Win: {avg_win:.1f}% | Avg Loss: {avg_loss:.1f}%", flush=True)

# ===== GSHEET UPDATE - NaN/INF FIX =====
try:
    summary = pd.DataFrame([{
        'Total_Stocks': len(stocks), 'Total_Setups': total, 'First_BO': first_bo_count, 'Re_BO': rebo_count,
        'Winrate_%': winrate, 'Avg_Win_%': round(avg_win, 1), 'Avg_Loss_%': round(avg_loss, 1),
        'Total_PnL_%': round(total_pnl, 1), 'Max_Drawdown_%': max_drawdown, 'Strategy': 'V8.6 RS WINDOW'
    }])

    # *** FIX: Replace inf and NaN with 0 before sending to GSheet ***
    df_res = df_res.replace([np.inf, -np.inf], np.nan).fillna(0)
    df_fund = df_fund.replace([np.inf, -np.inf], np.nan).fillna(0)
    summary = summary.replace([np.inf, -np.inf], np.nan).fillna(0)

    def get_or_create_ws(sh, title):
        try: return sh.worksheet(title)
        except: return sh.add_worksheet(title=title, rows=5000, cols=30)

    ws_trades = get_or_create_ws(sh, "QFACTOR_V8_6_TRADES")
    ws_trades.clear()
    if not df_res.empty: ws_trades.update([df_res.columns.values.tolist()] + df_res.values.tolist())

    ws_summary = get_or_create_ws(sh, "SUMMARY_V8_6")
    ws_summary.clear()
    ws_summary.update([summary.columns.values.tolist()] + summary.values.tolist())

    ws_fund = get_or_create_ws(sh, "FUNDAMENTAL_DEBUG_V8_6")
    ws_fund.clear()
    ws_fund.update([df_fund.columns.values.tolist()] + df_fund.values.tolist())

    print("GSheet updated successfully", flush=True)
except Exception as e:
    print(f"GSheet update failed: {str(e)[:100]}", flush=True)
