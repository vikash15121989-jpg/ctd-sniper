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
print(f"=== [LIQUIDITY & CONTINUOUS 3% TRAILING STOPLOSS BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
print(f"📥 Scanning {len(clean_stocks)} stocks with Continuous 3% Trailing Stoploss...", flush=True)

all_summary_rows = []
all_trade_details = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        data = yf.Ticker(symbol).history(period="max", interval="1d")
        if data.empty or len(data) < 250:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        # Resample to Monthly for Setup Identification
        monthly = data.resample('ME').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()

        trades = []

        for i in range(4, len(monthly) - 1):
            prev_month = monthly.iloc[i-1]    
            setup_month = monthly.iloc[i]     
            
            # Liquidity Filter
            setup_vol = float(setup_month['Volume'])
            setup_close = float(setup_month['Close'])
            setup_turnover = setup_vol * setup_close
            
            if setup_vol < 1000000 or setup_turnover < 100000000:
                continue

            cond_close_above_high = setup_close > prev_month['High']
            prev_4_vols = monthly['Volume'].iloc[i-4:i]
            cond_volume_surge = setup_vol > prev_4_vols.max()
            
            if cond_close_above_high and cond_volume_surge:
                entry_level = setup_month['High']
                initial_sl = setup_month['Low']
                setup_date = monthly.index[i].strftime('%Y-%m')
                
                trade_month_start = monthly.index[i+1].strftime('%Y-%m-01')
                trade_month_end = (monthly.index[i+1] + pd.offsets.MonthEnd(0)).strftime('%Y-%m-%d')
                trade_date = monthly.index[i+1].strftime('%Y-%m')
                
                daily_trade_df = data.loc[trade_month_start:trade_month_end]
                if daily_trade_df.empty:
                    continue
                
                triggered = False
                entry_price = entry_level
                highest_price_seen = entry_price
                current_sl = initial_sl
                exit_price = entry_price
                status = 'No Trigger'
                pnl_pct = 0.0
                
                for idx, day in daily_trade_df.iterrows():
                    d_high = float(day['High'])
                    d_low = float(day['Low'])
                    d_close = float(day['Close'])
                    
                    if not triggered:
                        if d_high >= entry_level:
                            triggered = True
                            highest_price_seen = max(entry_price, d_high)
                            # Continuous 3% Trailing SL from high
                            trail_sl = highest_price_seen * 0.97
                            current_sl = max(initial_sl, trail_sl)
                            
                            # Initial Stoploss check on trigger day
                            if d_low <= current_sl:
                                exit_price = current_sl
                                status = 'Stoploss Hit'
                                pnl_pct = ((current_sl - entry_price) / entry_price) * 100
                                break
                    else:
                        # Check Trailing Stoploss Hit
                        if d_low <= current_sl:
                            exit_price = current_sl
                            status = 'Trailing 3% SL Hit' if current_sl > initial_sl else 'Initial SL Hit'
                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            break
                        
                        # Update Highest High & Trail SL by 3%
                        if d_high > highest_price_seen:
                            highest_price_seen = d_high
                            new_trail_sl = highest_price_seen * 0.97 # 3% trail
                            if new_trail_sl > current_sl:
                                current_sl = new_trail_sl

                # If month ended without hitting SL, exit at last day close
                if triggered and (status == 'No Trigger' or 'SL Hit' not in status):
                    final_close = float(daily_trade_df.iloc[-1]['Close'])
                    exit_price = final_close
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    status = 'Closed at Month End'

                if triggered:
                    trades.append({
                        'Stock': stock_clean,
                        'Setup_Month': setup_date,
                        'Trade_Month': trade_date,
                        'Entry_Price': round(entry_price, 2),
                        'StopLoss': round(initial_sl, 2),
                        'Exit_Price': round(exit_price, 2),
                        'Status': status,
                        'PnL_%': round(pnl_pct, 2)
                    })

        if trades:
            df_trades = pd.DataFrame(trades)
            for _, tr in df_trades.iterrows():
                all_trade_details.append([
                    tr['Stock'], tr['Setup_Month'], tr['Trade_Month'], 
                    tr['Entry_Price'], tr['StopLoss'], tr['Exit_Price'], 
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

# 2. OVERALL PORTFOLIO METRICS CALCULATION
if all_trade_details:
    df_all_trades = pd.DataFrame(all_trade_details, columns=[
        'Stock', 'Setup_Month', 'Trade_Month', 'Entry_Price', 'StopLoss', 'Exit_Price', 'Status', 'PnL_%'
    ])
    
    overall_total_trades = len(df_all_trades)
    overall_wins = len(df_all_trades[df_all_trades['PnL_%'] > 0])
    overall_losses = len(df_all_trades[df_all_trades['PnL_%'] <= 0])
    overall_win_rate = (overall_wins / overall_total_trades) * 100 if overall_total_trades > 0 else 0

    ws_overall = get_or_create_worksheet("Monthly_Overall_Summary")
    ws_overall.clear()
    
    overall_data = [
        ["METRIC", "VALUE"],
        ["Total Stocks Scanned", len(clean_stocks)],
        ["Total Trades Generated (3% Continuous Trail)", overall_total_trades],
        ["Total Winning Trades", overall_wins],
        ["Total Losing Trades", overall_losses],
        ["OVERALL WIN RATE (%)", f"{overall_win_rate:.2f}%"],
        ["Total Cumulative Return (%)", f"{df_all_trades['PnL_%'].sum():.2f}%"]
    ]
    ws_overall.append_rows(overall_data, value_input_option="USER_ENTERED")
    print("🏆 Overall Metrics Exported with Continuous 3% Trailing Stoploss!")

# 3. EXPORT INDIVIDUAL & TRADE LOGS
ws_summary = get_or_create_worksheet("Monthly_Strategy_Summary")
ws_summary.clear()
ws_summary.append_row(["Stock", "Total_Trades", "Winning_Trades", "Losing_Trades", "Win_Rate_%", "Avg_PnL_%", "Total_Cumulative_PnL_%"], value_input_option="USER_ENTERED")
if all_summary_rows:
    ws_summary.append_rows(sanitize_rows(all_summary_rows), value_input_option="USER_ENTERED")

ws_logs = get_or_create_worksheet("Monthly_Trade_Logs")
ws_logs.clear()
ws_logs.append_row(["Stock", "Setup_Month", "Trade_Month", "Entry_Price", "StopLoss", "Exit_Price", "Status", "PnL_%"], value_input_option="USER_ENTERED")
if all_trade_details:
    ws_logs.append_rows(sanitize_rows(all_trade_details), value_input_option="USER_ENTERED")
    
