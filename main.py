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

print("=== VA-PA Q-FACTOR V8.3 PRICE ACTION LIVE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# Price Action Settings & Rules
R = {
    'min_price': 50,
    'max_hold_days': 30,
    'cooldown_days': 10,
    'target_pct': 0.10,     # 10% Target
    'sl_loss_pct': 0.05,     # 5% Stop Loss
    'rs_days': 30
}

today = datetime.now().date()

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
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif sl_hit:
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']
                exit_status = 'WIN'
            else:
                # Max Hold Days check (30 days logic from Price Action)
                entry_date = datetime.strptime(str(pos['Entry_Date']), '%Y-%m-%d').date()
                days_held = (today - entry_date).days
                if days_held >= R['max_hold_days']:
                    exit_price = today_close
                    exit_status = 'TIMEOUT'
                else:
                    continue  # Still open
            
            pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
            pnl_rs = round((exit_price - pos['Entry']) * (pos['Qty'] if pos['Qty'] else 1), 0)
            
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

# ===== 5. NEW PRICE ACTION INDICATORS & LOGIC =====
def build_indicators(df):
    if len(df) < 21: return df
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    return df

def check_price_action_signal(df):
    """Last row par criteria check karega"""
    if len(df) < 22: return False, None
    
    row = df.iloc[-1]
    row_prev = df.iloc[-2]
    is_green = row['Close'] > row['Open']
    
    # STRATEGY 1: PA_SUPPORT_RETEST
    low_near_support = ((row['Low'] / row['Support_20D']) - 1) * 100 <= 2.0
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    strong_rejection = lower_wick >= (body * 1.0)
    
    if low_near_support and (strong_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # STRATEGY 2: PA_CHoCH_BREAKOUT
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    strong_volume = row['Vol_Multiple'] > 1.25
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, None

# ===== 6. SCAN FOR NEW SIGNALS - ONLY IF RS WINDOW ACTIVE =====
new_signals = []
if rs_window_active:
    # Watchlist sheet se tickers read karna
    stocks = ws_watchlist.col_values(1)[1:]
    stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))
    
    # Header cleaning
    if stocks and stocks[0] in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks = stocks[1:]
        
    # Open positions wale stocks skip karo
    open_stocks = open_trades['Stock'].tolist() if not open_trades.empty else []
    stocks = [s for s in stocks if s not in open_stocks]
    
    print(f"\nScanning {len(stocks)} Watchlist stocks for new Price Action entries...", flush=True)
    
    for stock in stocks:
        try:
            ticker_formatted = f"{stock}.NS" if not stock.endswith(".NS") else stock
            df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if df.empty or len(df) < 30: continue
            
            # Indicators build karo
            df = build_indicators(df)
            row = df.iloc[-1]
            
            # Minimum Price check
            if row['Close'] < R['min_price']: continue
            
            # Price Action logic verification
            is_signal, mode = check_price_action_signal(df)
            
            if is_signal:
                entry_price = row['Close']
                # Target fix 10% aur SL fix 5% system as per strategy rules
                target_price = entry_price * (1 + R['target_pct'])
                sl_price = entry_price * (1 - R['sl_loss_pct'])
                
                new_signals.append({
                    'Stock': stock.replace(".NS", ""),
                    'Entry_Date': today.strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2),
                    'SL': round(sl_price, 2),
                    'Target': round(target_price, 2),
                    'Qty': 10, # Default Qty setup, sheet calculation ke hisab se badal sakte hain
                    'Status': 'OPEN',
                    'Exit_Date': 'N/A',
                    'Exit_Price': 'N/A',
                    'PnL_%': 'N/A',
                    'PnL_Rs': 'N/A',
                    'Strategy_Mode': mode
                })
                print(f"SIGNAL: {stock} [{mode}] | Entry: {entry_price:.2f} | SL: {sl_price:.2f} | Target: {target_price:.2f}", flush=True)
                
            time.sleep(0.1) # Yahoo safe boundary buffer
        except Exception as e:
            continue
else:
    print(f"\nRS Window closed. No new trades. Only managing existing positions.", flush=True)

# ===== 7. UPDATE GSHEET =====
try:
    # 1. Update live trades tab
    ws_live.clear()
    if not df_live.empty:
        ws_live.update([df_live.columns.values.tolist()] + df_live.values.tolist())
    
    # 2. Update New signals sheet (Isme pure schema matching rows jayenge)
    ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
    ws_signals.clear()
    if new_signals:
        df_signals = pd.DataFrame(new_signals)
        ws_signals.update([df_signals.columns.values.tolist()] + df_signals.values.tolist())
        
        # Auto-append signals to Live Trades if you want them trackable
        # df_live = pd.concat([df_live, df_signals.drop(columns=['Strategy_Mode'])], ignore_index=True)
        # ws_live.clear()
        # ws_live.update([df_live.columns.values.tolist()] + df_live.values.tolist())
    else:
        ws_signals.update([['No new signals today']])
    
    # 3. Summary metrics sheet generation
    total_trades = len(df_live)
    open_count = len(df_live[df_live['Status'] == 'OPEN']) if total_trades else 0
    closed_trades = df_live[df_live['Status'].isin(['WIN', 'LOSS', 'TIMEOUT'])] if total_trades else pd.DataFrame()
    wins = len(closed_trades[closed_trades['Status'] == 'WIN']) if not closed_trades.empty else 0
    total_closed = len(closed_trades) if not closed_trades.empty else 0
    winrate = round(wins / total_closed * 100, 1) if total_closed else 0
    
    # PnL computation safely converting strings if any
    total_pnl = 0
    if not closed_trades.empty:
        total_pnl = pd.to_numeric(closed_trades['PnL_Rs'], errors='coerce').sum()
    
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
    print(f"LIVE_TRADES_V8_3: {len(df_live)} rows updated", flush=True)
    print(f"NEW_SIGNALS_TODAY: {len(new_signals)} signals found", flush=True)
    print(f"LIVE_SUMMARY: Updated successfully", flush=True)
    
except Exception as e:
    print(f"GSheet update failed: {str(e)}", flush=True)

print(f"\n=== LIVE RUN COMPLETE ===", flush=True)
