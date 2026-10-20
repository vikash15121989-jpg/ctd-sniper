import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== LIQUIDITY FILTER + MULTI-FILTER PNL BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def run_liquidity_and_pnl_backtest(symbol):
    try:
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=365)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 60:
            return []
            
        df.columns = [str(c).strip().title() for c in df.columns]
        df = df.loc[:, ~df.columns.duplicated()]
        
        col_map = {}
        for c in df.columns:
            clow = c.lower()
            if 'total traded quantity' in clow or 'quantity' in clow:
                col_map[c] = 'Volume'
            elif 'no. of trades' in clow or 'trades' in clow:
                col_map[c] = 'Trades'
            elif 'close' in clow:
                col_map[c] = 'Close'
            elif 'high' in clow:
                col_map[c] = 'High'
            elif 'low' in clow:
                col_map[c] = 'Low'
            elif 'date' in clow:
                col_map[c] = 'Date'

        df = df.rename(columns=col_map)
        df = df.loc[:, ~df.columns.duplicated()]
        
        required_cols = ['Volume', 'Trades', 'Close', 'High', 'Low', 'Date']
        if not all(col in df.columns for col in required_cols):
            return []

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        # Calculate Turnover (Value Traded in Rupees)
        df['Turnover'] = df['Volume'] * df['Close']
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        df['EMA5'] = df['Close'].ewm(span=5).mean()
        
        trade_rows = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 20):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger
            if vol >= (2.5 * vol_ma):
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                # --- LIQUIDITY FILTER CHECK ---
                # Average daily turnover in the last 10 days must be >= ₹2 Crores (20,000,000 INR)
                avg_10d_turnover = pre_10['Turnover'].mean()
                if avg_10d_turnover < 20000000: 
                    continue # Skip illiquid stocks
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    avg_tightness = pre_10['Candle_Range_%'].mean()
                    spike_days_cnt = ((pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']) >= 1.5).sum()
                    
                    green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    green_red_ratio = green_vol / red_vol if red_vol > 0 else 1.0
                    
                    # Multi-Filters Checklist
                    if avg_tightness <= 4.0 and spike_days_cnt >= 2 and green_red_ratio >= 2.5:
                        range_high = pre_10['High'].max()
                        
                        for idx_ahead in range(i+1, min(i+8, n_rows - 15)):
                            if df['Close'].iloc[idx_ahead] > range_high:
                                entry_date = df['Date'].iloc[idx_ahead].strftime('%Y-%m-%d')
                                entry_p = range_high
                                
                                future_15 = df.iloc[idx_ahead+1 : idx_ahead+16]
                                if future_15.empty:
                                    break
                                
                                # Scenario 1: Fixed 6% Target / 3% SL
                                sl_s1 = entry_p * 0.97
                                tgt_s1 = entry_p * 1.06
                                pnl_s1 = -3.0
                                for _, f in future_15.iterrows():
                                    if f['High'] >= tgt_s1:
                                        pnl_s1 = 6.0
                                        break
                                    elif f['Low'] <= sl_s1:
                                        pnl_s1 = -3.0
                                        break
                                
                                # Scenario 2: Trailing SL (5 EMA Close Exit)
                                pnl_s2 = -3.0
                                trailing_sl = entry_p * 0.97
                                for _, f in future_15.iterrows():
                                    if f['Close'] < f['EMA5'] and f['Close'] > entry_p:
                                        pnl_s2 = round((f['Close'] - entry_p) / entry_p * 100, 2)
                                        break
                                    elif f['Low'] <= trailing_sl:
                                        pnl_s2 = -3.0
                                        break
                                else:
                                    pnl_s2 = round((future_15['High'].max() - entry_p) / entry_p * 100, 2)
                                
                                trade_rows.append({
                                    "Stock": symbol,
                                    "Entry_Date": entry_date,
                                    "Entry_Price": round(entry_p, 2),
                                    "Avg_Turnover_Cr": round(avg_10d_turnover / 10000000, 2),
                                    "S1_Fixed6%_PnL": pnl_s1,
                                    "S2_Trailing_PnL": pnl_s2
                                })
                                break
                        i += 15
        return trade_rows
    except Exception as e:
        return []

all_liquid_trades = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Running liquidity + PnL backtest for {sym}...")
        res = run_liquidity_and_pnl_backtest(sym)
        if res:
            all_liquid_trades.extend(res)

df_liq = pd.DataFrame(all_liquid_trades)

if not df_liq.empty:
    tot = len(df_liq)
    s1_tot = round(df_liq['S1_Fixed6%_PnL'].sum(), 2)
    s2_tot = round(df_liq['S2_Trailing_PnL'].sum(), 2)
    
    print("\n" + "="*65)
    print("   LIQUIDITY FILTERED PNL REPORT (>= ₹2Cr Turnover)   ")
    print("="*65)
    print(f"Total Liquid Filtered Trades   : {tot}")
    print(f"Fixed 6% Target + 3% SL P&L    : +{s1_tot}% (Avg: +{round(s1_tot/tot,2)}% per trade)")
    print(f"Trailing SL Strategy P&L       : +{s2_tot}% (Avg: +{round(s2_tot/tot,2)}% per trade)")
    print("="*65)

    try:
        ws = sh.worksheet("LIQUID_FILTERED_PNL") if "LIQUID_FILTERED_PNL" in [x.title for x in sh.worksheets()] else sh.add_worksheet("LIQUID_FILTERED_PNL", 2000, 10)
        ws.clear()
        
        ws.append_row([f"LIQUIDITY FILTERED PNL REPORT | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Trades", "Fixed 6% Total P&L", "Trailing SL Total P&L"])
        ws.append_row([tot, f"+{s1_tot}%", f"+{s2_tot}%"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Entry Date", "Entry Price", "Avg Turnover (Cr)", "Fixed 6% PnL %", "Trailing PnL %"])
        ws.append_rows(df_liq.values.tolist())
        print("✅ Liquidity filtered report exported to Google Sheet tab 'LIQUID_FILTERED_PNL'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No trades matched the liquidity and multi-filter criteria.")
    
