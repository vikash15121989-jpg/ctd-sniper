from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import numpy as np
import yfinance as yf

# IST Timezone setup
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(f"=== [MULTI-LAYER BACKTEST DEBUG SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(v) for v in row] for row in rows]

# Google Sheets Connection
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheets!", flush=True)
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="5000", cols="15")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# 1. Read Watchlist from Sheet
raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Scanning {len(clean_stocks)} stocks with Multi-Layer Logic...", flush=True)

all_summary_rows = []
all_trade_details = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        data = yf.Ticker(symbol).history(period="max", interval="1d")
        if data.empty or len(data) < 500:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        monthly = data.resample('ME').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()

        weekly = data.resample('W-FRI').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()

        if len(monthly) < 6 or len(weekly) < 10:
            continue

        trades = []

        for j in range(4, len(weekly) - 1):
            prev_w = weekly.iloc[j-1]
            setup_w = weekly.iloc[j]
            
            w_vol = float(setup_w['Volume'])
            w_close = float(setup_w['Close'])
            if w_vol < 200000 or (w_vol * w_close) < 20000000:
                continue

            # Weekly Check
            w_cond_close = w_close > prev_w['High']
            w_prev_vols = weekly['Volume'].iloc[j-4:j]
            w_cond_vol = w_vol > w_prev_vols.max()

            if not (w_cond_close and w_cond_vol):
                continue

            # Monthly Check
            setup_w_date = weekly.index[j]
            matching_months = monthly[monthly.index <= setup_w_date]
            if len(matching_months) < 5:
                continue
            
            m_idx = len(matching_months) - 1
            prev_m = monthly.iloc[m_idx - 1]
            setup_m = monthly.iloc[m_idx]

            m_cond_close = setup_m['Close'] > prev_m['High']
            m_prev_vols = monthly['Volume'].iloc[m_idx-4:m_idx] if m_idx >= 4 else monthly['Volume'].iloc[0:m_idx]
            m_cond_vol = setup_m['Volume'] > m_prev_vols.max() if len(m_prev_vols) > 0 else True

            if m_cond_close and m_cond_vol:
                entry_level = setup_w['High']
                initial_sl = setup_w['Low']
                setup_date = setup_w_date.strftime('%Y-%m-%d')
                
                trade_week_start = weekly.index[j] + timedelta(days=1)
                trade_week_end = weekly.index[j+1] + timedelta(days=5)
                daily_trade_df = data.loc[trade_week_start.strftime('%Y-%m-%d'):trade_week_end.strftime('%Y-%m-%d')]
                if daily_trade_df.empty:
                    continue
                
                triggered = False
                entry_price = entry_level
                target_price = entry_price * 1.06
                current_sl = initial_sl
                exit_price = entry_price
                status = 'No Trigger'
                pnl_pct = 0.0
                
                for idx, day in daily_trade_df.iterrows():
                    d_high = float(day['High'])
                    d_low = float(day['Low'])
                    
                    if not triggered:
                        if d_high >= entry_level:
                            triggered = True
                            if d_high >= target_price:
                                exit_price = target_price
                                status = '6% Target Hit'
                                pnl_pct = 6.0
                                break
                            elif d_low <= current_sl:
                                exit_price = current_sl
                                status = 'Stoploss Hit'
                                pnl_pct = ((current_sl - entry_price) / entry_price) * 100
                                break
                    else:
                        if d_high >= target_price:
                            exit_price = target_price
                            status = '6% Target Hit'
                            pnl_pct = 6.0
                            break
                        elif d_low <= current_sl:
                            exit_price = current_sl
                            status = 'Stoploss Hit'
                            pnl_pct = ((current_sl - entry_price) / entry_price) * 100
                            break

                if triggered and status == 'No Trigger':
                    final_close = float(daily_trade_df.iloc[-1]['Close'])
                    exit_price = final_close
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    status = '6% Target Hit' if pnl_pct >= 6.0 else ('Stoploss Hit' if pnl_pct < 0 else 'Closed in Profit')

                if triggered:
                    trades.append({
                        'Stock': stock_clean,
                        'Setup_Week': setup_date,
                        'Trade_Week': weekly.index[j+1].strftime('%Y-%m-%d'),
                        'Entry_Price': round(entry_price, 2),
                        'Target_6%': round(target_price, 2),
                        'StopLoss': round(initial_sl, 2),
                        'Exit_Price': round(exit_price, 2),
                        'Status': status,
                        'PnL_%': round(pnl_pct, 2)
                    })

        if trades:
            df_trades = pd.DataFrame(trades)
            for _, tr in df_trades.iterrows():
                all_trade_details.append([
                    tr['Stock'], tr['Setup_Week'], tr['Trade_Week'], 
                    tr['Entry_Price'], tr['Target_6%'], tr['StopLoss'], tr['Exit_Price'], 
                    tr['Status'], tr['PnL_%']
                ])
            
            total_trades = len(df_trades)
            wins = len(df_trades[df_trades['PnL_%'] > 0])
            losses = len(df_trades[df_trades['PnL_%'] <= 0])
            win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
            avg_pnl = df_trades['PnL_%'].mean()
            total_cum_pnl = df_trades['PnL_%'].sum()
            
            all_summary_rows.append([
                stock_clean, total_trades, wins, losses, 
                round(win_rate, 2), round(avg_pnl, 2), round(total_cum_pnl, 2)
            ])

    except Exception:
        continue

print(f"📊 Total Multi-Layer Trades Found Across All Stocks: {len(all_trade_details)}", flush=True)

# 2. CREATE A BRAND NEW DEDICATED TAB: "MultiLayer_Summary"
if all_trade_details:
    df_all_trades = pd.DataFrame(all_trade_details, columns=[
        'Stock', 'Setup_Week', 'Trade_Week', 'Entry_Price', 'Target_6%', 'StopLoss', 'Exit_Price', 'Status', 'PnL_%'
    ])
    
    overall_total_trades = len(df_all_trades)
    target_hit_count = len(df_all_trades[df_all_trades['PnL_%'] >= 6.0])
    overall_wins = len(df_all_trades[df_all_trades['PnL_%'] > 0])
    overall_losses = len(df_all_trades[df_all_trades['PnL_%'] <= 0])
    
    overall_win_rate = (overall_wins / overall_total_trades) * 100 if overall_total_trades > 0 else 0
    target_hit_pct = (target_hit_count / overall_total_trades) * 100 if overall_total_trades > 0 else 0
    loss_pct = (overall_losses / overall_total_trades) * 100 if overall_total_trades > 0 else 0

    ws_new = get_or_create_worksheet("MultiLayer_Summary")
    ws_new.clear()
    
    overall_data = [
        ["METRIC", "VALUE"],
        ["Strategy Type", "Multi-Layer (Monthly Macro + Weekly Micro)"],
        ["Total Stocks Scanned", len(clean_stocks)],
        ["Total Trades Generated", overall_total_trades],
        ["Trades Hit 6% Target", f"{target_hit_count} ({target_hit_pct:.2f}%)"],
        ["Trades Hit Stop Loss", f"{overall_losses} ({loss_pct:.2f}%)"],
        ["OVERALL WIN RATE (%)", f"{overall_win_rate:.2f}%"],
        ["Total Cumulative Return (%)", f"{df_all_trades['PnL_%'].sum():.2f}%"]
    ]
    ws_new.append_rows(overall_data, value_input_option="USER_ENTERED")
    print("🏆 Brand New Tab 'MultiLayer_Summary' Created and Exported Successfully!", flush=True)
else:
    print("⚠️ Warning: No trades were generated with this multi-layer filter combination across the scanned stocks.", flush=True)
    
