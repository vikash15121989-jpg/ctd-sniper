import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA Q-FACTOR V8.3 LIVE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {'min_price': 50, 'min_daily_value_cr': 0.5, 'sl_buffer_pct': 3.0, 'target_r': 1.0, 'max_risk_pct': 25.0, 'vol_blast_ratio': 1.2, 'rs_days': 30}
F = {'min_market_cap_cr': 500, 'max_debt_equity': 10.0, 'max_pe': 1000}

today = datetime.now().date()
lookback_days = 400

# ===== 2. NIFTY RS CHECK =====
nifty = yf.download("^NSEI", period="2y", progress=False, auto_adjust=True)
if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.droplevel(1)
nifty['52W_High'] = nifty['High'].rolling(252, min_periods=252).max()
nifty_52h_date = nifty['52W_High'].idxmax()
days_since_52h = (nifty.index[-1] - nifty_52h_date).days
rs_window_active = days_since_52h <= R['rs_days']

print(f"\n=== NIFTY STATUS ===", flush=True)
print(f"Last 52W High: {nifty_52h_date.strftime('%Y-%m-%d')} | Days Ago: {days_since_52h}", flush=True)
print(f"RS Window: {'ACTIVE' if rs_window_active else 'CLOSED'} | New trades: {'YES' if rs_window_active else 'NO'}", flush=True)

# ===== 3. LOAD EXISTING POSITIONS =====
def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=30)

ws_live = get_or_create_ws(sh, "LIVE_TRADES_V8_3")
try:
    df_live = pd.DataFrame(ws_live.get_all_records())
    if df_live.empty: df_live = pd.DataFrame(columns=['Stock','Entry_Date','Entry','SL','Target','Qty','Status','Exit_Date','Exit_Price','PnL_%','PnL_Rs'])
except:
    df_live = pd.DataFrame(columns=['Stock','Entry_Date','Entry','SL','Target','Qty','Status','Exit_Date','Exit_Price','PnL_%','PnL_Rs'])

open_trades = df_live[df_live['Status'] == 'OPEN'].copy() if not df_live.empty else pd.DataFrame()
print(f"\nOpen Positions: {len(open_trades)}", flush=True)

# ===== 4. CHECK OPEN POSITIONS FOR SL/TARGET =====
exits_today = []
if not open_trades.empty:
    for idx, pos in open_trades.iterrows():
        stock = pos['Stock']
        try:
            data = yf.download(f"{stock}.NS", period="5d", progress=False, auto_adjust=True)
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            if data.empty: continue
            
            today_low = data['Low'].iloc[-1]
            today_high = data['High'].iloc[-1]
            today_close = data['Close'].iloc[-1]
            
            sl_hit = today_low <= pos['SL']
            target_hit = today_high >= pos['Target']
            
            if sl_hit and target_hit:
                # Dono hit. Gap down open. SL priority
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif sl_hit:
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']
                exit_status = 'WIN'
            else:
                # 60 day check
                entry_date = datetime.strptime(pos['Entry_Date'], '%Y-%m-%d').date()
                days_held = (today - entry_date).days
                if days_held >= 60:
                    exit_price = today_close
                    exit_status = 'TIME'
                else:
                    continue  # Still open
            
            pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
            pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
            
            exits_today.append({
                'Stock': stock, 'Exit_Date': today.strftime('%Y-%m-%d'), 
                'Exit_Price': round(exit_price, 2), 'Status': exit_status,
                'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs, 'Index': idx
            })
            print(f"EXIT: {stock} | {exit_status} | {pnl_pct}% | Rs.{pnl_rs}", flush=True)
        except Exception as e:
            print(f"Error checking {stock}: {str(e)[:50]}", flush=True)

# Update exits in df_live
for ex in exits_today:
    df_live.loc[ex['Index'], 'Status'] = ex['Status']
    df_live.loc[ex['Index'], 'Exit_Date'] = ex['Exit_Date']
    df_live.loc[ex['Index'], 'Exit_Price'] = ex['Exit_Price']
    df_live.loc[ex['Index'], 'PnL_%'] = ex['PnL_%']
    df_live.loc[ex['Index'], 'PnL_Rs'] = ex['PnL_Rs']

# ===== 5. SCAN FOR NEW SIGNALS - ONLY IF RS WINDOW ACTIVE =====
new_signals = []
if rs_window_active:
    stocks = ws_watchlist.col_values(1)[1:]
    stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))
    
    # Already open stocks skip karo
    open_stocks = open_trades['Stock'].tolist() if not open_trades.empty else []
    stocks = [s for s in stocks if s not in open_stocks]
    
    print(f"\nScanning {len(stocks)} stocks for new entries...", flush=True)
    
    for stock in stocks:
        try:
            df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) < 300: continue
            
            # Check today only
            row = df.iloc[-1]
            i = len(df) - 1
            
            # Price filter
            if row['Close'] < R['min_price']: continue
            
            # Liquidity filter
            avg_value_cr = (df['Close'].iloc[i-20:i] * df['Volume'].iloc[i-20:i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']: continue
            
            # 52W BO filter
            high_252 = df['High'].iloc[i-252:i].max()
            if pd.isna(high_252) or row['Close'] <= high_252 * 1.01: continue
            if df['Close'].iloc[i-1] > high_252 * 1.01: continue  # Aaj hi BO hona chahiye
            
            # Volume blast filter
            avg_vol_20 = df['Volume'].iloc[i-20:i].mean()
            if avg_vol_20 == 0 or pd.isna(avg_vol_20): avg_vol_20 = 1
            vol_ratio = row['Volume'] / avg_vol_20
            if vol_ratio < R['vol_blast_ratio']: continue
            
            # Fundamental check
            t = yf.Ticker(f"{stock}.NS")
            info = t.info
            mcap_cr = info.get('marketCap', 0) / 1e7
            de = info.get('debtToEquity', 999)
            pe = info.get('trailingPE', 999)
            if mcap_cr < F['min_market_cap_cr'] or de > F['max_debt_equity'] or pe > F['max_pe']: continue
            
            # Calculate SL/Target
            entry_price = row['Close']
            sl_price = df['Low'].iloc[i-20:i+1].min() * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue
            target = entry_price + risk * R['target_r']
            
            new_signals.append({
                'Stock': stock,
                'Entry_Date': today.strftime('%Y-%m-%d'),
                'Entry': round(entry_price, 2),
                'SL': round(sl_price, 2),
                'Target': round(target, 2),
                'Risk_%': round(risk_pct, 1),
                'Vol_X': round(vol_ratio, 1),
                '52W_High': round(high_252, 2),
                'Mcap_Cr': round(mcap_cr, 0),
                'DE': round(de, 1),
                'PE': round(pe, 1)
            })
            print(f"SIGNAL: {stock} | Entry: {entry_price:.1f} | SL: {sl_price:.1f} | Target: {target:.1f} | Risk: {risk_pct:.1f}%", flush=True)
            
        except Exception as e:
            continue
else:
    print(f"\nRS Window closed. No new trades. Only managing existing positions.", flush=True)

# ===== 6. UPDATE GSHEET =====
try:
    # Update live trades
    ws_live.clear()
    if not df_live.empty:
        ws_live.update([df_live.columns.values.tolist()] + df_live.values.tolist())
    
    # New signals sheet
    ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
    ws_signals.clear()
    if new_signals:
        df_signals = pd.DataFrame(new_signals)
        ws_signals.update([df_signals.columns.values.tolist()] + df_signals.values.tolist())
    else:
        ws_signals.update([['No new signals today']])
    
    # Summary sheet
    total_trades = len(df_live)
    open_count = len(df_live[df_live['Status'] == 'OPEN']) if total_trades else 0
    closed_trades = df_live[df_live['Status'] != 'OPEN'] if total_trades else pd.DataFrame()
    wins = len(closed_trades[closed_trades['Status'] == 'WIN']) if not closed_trades.empty else 0
    total_closed = len(closed_trades) if not closed_trades.empty else 0
    winrate = round(wins / total_closed * 100, 1) if total_closed else 0
    total_pnl = closed_trades['PnL_Rs'].sum() if not closed_trades.empty else 0
    
    summary = pd.DataFrame([{
        'Date': today.strftime('%Y-%m-%d'),
        'Nifty_52H_Date': nifty_52h_date.strftime('%Y-%m-%d'),
        'Days_Since_52H': days_since_52h,
        'RS_Window': 'ACTIVE' if rs_window_active else 'CLOSED',
        'Total_Trades': total_trades,
        'Open_Positions': open_count,
        'Closed_Trades': total_closed,
        'Winrate_%': winrate,
        'Total_PnL_Rs': round(total_pnl, 0),
        'New_Signals_Today': len(new_signals),
        'Exits_Today': len(exits_today)
    }])
    
    ws_summary = get_or_create_ws(sh, "LIVE_SUMMARY")
    ws_summary.clear()
    ws_summary.update([summary.columns.values.tolist()] + summary.values.tolist())
    
    print(f"\n=== GSHEET UPDATED ===", flush=True)
    print(f"LIVE_TRADES_V8_3: {len(df_live)} rows", flush=True)
    print(f"NEW_SIGNALS_TODAY: {len(new_signals)} signals", flush=True)
    print(f"LIVE_SUMMARY: Updated", flush=True)
    
except Exception as e:
    print(f"GSheet update failed: {str(e)[:100]}", flush=True)

print(f"\n=== LIVE RUN COMPLETE ===", flush=True)
