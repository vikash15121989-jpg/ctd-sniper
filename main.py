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
print(f"=== [LIQUIDITY & 6% TARGET MONTHLY BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
print(f"📥 Scanning {len(clean_stocks)} stocks with Liquidity & 6% Target Rule...", flush=True)

all_summary_rows = []
all_trade_details = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        # Download Daily Data & Resample to Monthly
        data = yf.Ticker(symbol).history(period="max", interval="1d")
        if data.empty or len(data) < 250:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        monthly = data.resample('ME').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()

        trades = []

        # Loop through monthly candles starting from index 4
        for i in range(4, len(monthly) - 1):
            prev_month = monthly.iloc[i-1]    
            setup_month = monthly.iloc[i]     
            
            # -------------------------------------------------------------
            # FILTER 1: LIQUIDITY CHECK (Minimum monthly volume & turnover)
            # -------------------------------------------------------------
            setup_vol = float(setup_month['Volume'])
            setup_close = float(setup_month['Close'])
            setup_turnover = setup_vol * setup_close
            
            # Minimum liquidity filter: e.g., Min 10 Lakh Volume & ₹10 Cr monthly turnover
            if setup_vol < 1000000 or setup_turnover < 100000000:
                continue

            # Condition 2: Setup month close > Previous month high
            cond_close_above_high = setup_close > prev_month['High']
            
            # Condition 3: Setup month volume > Max volume of previous 4 months
            prev_4_vols = monthly['Volume'].iloc[i-4:i]
            cond_volume_surge = setup_vol > prev_4_vols.max()
            
            if cond_close_above_high and cond_volume_surge:
                entry_level = setup_month['High']
                stoploss_level = setup_month['Low']
                setup_date = monthly.index[i].strftime('%Y-%m')
                
                trade_month = monthly.iloc[i+1] 
                trade_date = monthly.index[i+1].strftime('%Y-%m')
                
                # Entry condition: Next month high crosses setup month high
                if trade_month['High'] > entry_level:
                    entry_price = entry_level
                    target_price = entry_price * 1.06  # Fixed 6% Profit Target
                    
                    # Check outcomes in trade month
                    t_low = float(trade_month['Low'])
                    t_high = float(trade_month['High'])
                    t_close = float(trade_month['Close'])
                    
                    status = 'Loss'
                    exit_price = stoploss_level
                    pnl_pct = -100 * (entry_price - stoploss_level) / entry_price # approximate SL hit pct
                    
                    # Check if 6% Target hit before Stoploss
                    if t_high >= target_price:
                        status = '6% Target Hit (Profit)'
                        exit_price = target_price
                        pnl_pct = 6.0
                    elif t_low <= stoploss_level:
                        status = 'Stoploss Hit (Loss)'
                        exit_price = stoploss_level
                        pnl_pct = ((stoploss_level - entry_price) / entry_price) * 100
                    else:
                        # Exit at month close if neither hit
                        exit_price = t_close
                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                        status = '6% Target Hit (Profit)' if pnl_pct >= 6.0 else ('Stoploss Hit (Loss)' if pnl_pct < 0 else 'Closed in Profit')

                    trades.append({
                        'Stock': stock_clean,
                        'Setup_Month': setup_date,
                        'Trade_Month': trade_date,
                        'Entry_Price': round(entry_price, 2),
                        'Target_6%': round(target_price, 2),
                        'StopLoss': round(stoploss_level, 2),
                        'Exit_Price': round(exit_price, 2),
                        'Status': status,
                        'PnL_%': round(pnl_pct, 2)
                    })

        if trades:
            df_trades = pd.DataFrame(trades)
            for _, tr in df_trades.iterrows():
                all_trade_details.append([
                    tr['Stock'], tr['Setup_Month'], tr['Trade_Month'], 
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

# -------------------------------------------------------------
# 2. OVERALL PORTFOLIO METRICS CALCULATION
# -------------------------------------------------------------
if all_trade_details:
    df_all_trades = pd.DataFrame(all_trade_details, columns=[
        'Stock', 'Setup_Month', 'Trade_Month', 'Entry_Price', 'Target_6%', 'StopLoss', 'Exit_Price', 'Status', 'PnL_%'
    ])
    
    overall_total_trades = len(df_all_trades)
    target_hit_count = len(df_all_trades[df_all_trades['PnL_%'] >= 6.0])
    stoploss_hit_count = len(df_all_trades[df_all_trades['PnL_%'] < 0])
    other_wins = len(df_all_trades[(df_all_trades['PnL_%'] > 0) & (df_all_trades['PnL_%'] < 6.0)])
    
    overall_wins = len(df_all_trades[df_all_trades['PnL_%'] > 0])
    overall_losses = len(df_all_trades[df_all_trades['PnL_%'] <= 0])
    overall_win_rate = (overall_wins / overall_total_trades) * 100 if overall_total_trades > 0 else 0
    
    target_hit_pct = (target_hit_count / overall_total_trades) * 100 if overall_total_trades > 0 else 0
    loss_hit_pct = (overall_losses / overall_total_trades) * 100 if overall_total_trades > 0 else 0

    # Export Overall Summary to Google Sheet
    ws_overall = get_or_create_worksheet("Monthly_Overall_Summary")
    ws_overall.clear()
    
    overall_data = [
        ["METRIC", "VALUE"],
        ["Total Stocks Scanned (with Liquidity)", len(clean_stocks)],
        ["Total Trades Generated", overall_total_trades],
        ["Trades Hit 6% Target (Profit)", f"{target_hit_count} ({target_hit_pct:.2f}%)"],
        ["Trades Hit Stoploss / Loss", f"{overall_losses} ({loss_hit_pct:.2f}%)"],
        ["Other Profitable Trades", other_wins],
        ["OVERALL WIN RATE (%)", f"{overall_win_rate:.2f}%"],
        ["Total Cumulative Return (%)", f"{df_all_trades['PnL_%'].sum():.2f}%"]
    ]
    ws_overall.append_rows(overall_data, value_input_option="USER_ENTERED")
    print("🏆 Overall Metrics Exported with 6% Target Data!")

# -------------------------------------------------------------
# 3. EXPORT INDIVIDUAL & TRADE LOGS
# -------------------------------------------------------------
ws_summary = get_or_create_worksheet("Monthly_Strategy_Summary")
ws_summary.clear()
ws_summary.append_row(["Stock", "Total_Trades", "Winning_Trades", "Losing_Trades", "Win_Rate_%", "Avg_PnL_%", "Total_Cumulative_PnL_%"], value_input_option="USER_ENTERED")
if all_summary_rows:
    ws_summary.append_rows(sanitize_rows(all_summary_rows), value_input_option="USER_ENTERED")

ws_logs = get_or_create_worksheet("Monthly_Trade_Logs")
ws_logs.clear()
ws_logs.append_row(["Stock", "Setup_Month", "Trade_Month", "Entry_Price", "Target_6%", "StopLoss", "Exit_Price", "Status", "PnL_%"], value_input_option="USER_ENTERED")
if all_trade_details:
    ws_logs.append_rows(sanitize_rows(all_trade_details), value_input_option="USER_ENTERED")
    
