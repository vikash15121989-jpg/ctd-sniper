import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== OBV SNIPER: BUYER VS SELLER VOLUME ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']

ref_date = None
for fmt in date_formats:
    try:
        ref_date = datetime.strptime(date_raw, fmt)
        break
    except ValueError:
        continue

if ref_date is None:
    raise ValueError(f"A1 me date format galat: {date_raw}")

date_str = ref_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str}")

# 3. TERA BUYER/SELLER LOGIC ✅
def calculate_obv_buyer_seller(df):
    df = df.copy()
    df['OBV'] = 0
    df['Buyer_Vol'] = 0
    df['Seller_Vol'] = 0
    df['Cum_Buyer'] = 0
    df['Cum_Seller'] = 0

    for i in range(1, len(df)):
        # Normal OBV
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1] + df['Volume'].iloc[i]
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1] - df['Volume'].iloc[i]
        else:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1]

        # TERA FORMULA - GREEN HO YA RED ✅
        h, l, c, v = df['High'].iloc[i], df['Low'].iloc[i], df['Close'].iloc[i], df['Volume'].iloc[i]
        range_hl = h - l
        if range_hl == 0:
            buyer, seller = 0, 0
        else:
            buyer = (c - l) / range_hl * v # Low se Close = Buyer
            seller = (h - c) / range_hl * v # High se Close = Seller

        df.loc[df.index[i], 'Buyer_Vol'] = buyer
        df.loc[df.index[i], 'Seller_Vol'] = seller
        df.loc[df.index[i], 'Cum_Buyer'] = df['Cum_Buyer'].iloc[i-1] + buyer
        df.loc[df.index[i], 'Cum_Seller'] = df['Cum_Seller'].iloc[i-1] + seller

    df['OBV_20SMA'] = df['OBV'].rolling(20).mean()
    df['20DMA'] = df['Close'].rolling(20).mean()
    return df

# 4. OBV SCANNER - SIRF OBV + BUYER>SELLER
def find_obv_setup(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 50:
        return []

    df_till_date = calculate_obv_buyer_seller(df_till_date)
    setups = []

    for i in range(20, len(df_till_date)):
        curr = df_till_date.iloc[i]
        prev = df_till_date.iloc[i-1]

        # CONDITION-1: OBV > OBV_20SMA + OBV Rising
        if pd.isna(curr['OBV_20SMA']) or curr['OBV'] <= curr['OBV_20SMA'] or curr['OBV'] <= prev['OBV']:
            continue

        # CONDITION-2: 20 DAY CUMULATIVE BUYER > SELLER - TERA LOGIC ✅
        buyer_20d = curr['Cum_Buyer'] - df_till_date['Cum_Buyer'].iloc[i-20]
        seller_20d = curr['Cum_Seller'] - df_till_date['Cum_Seller'].iloc[i-20]

        if buyer_20d <= seller_20d:
            continue

        # CONDITION-3: Close > 20DMA
        if pd.isna(curr['20DMA']) or curr['Close'] <= curr['20DMA']:
            continue

        # ENTRY MILA
        entry_price = curr['Close']
        sl = curr['Low'] * 0.98 # 2% SL
        risk = entry_price - sl
        target = entry_price + (risk * 2) # 1:2 RR
        risk_pct = (risk / entry_price) * 100

        if risk_pct > 10:
            continue

        # BACKTEST - EXIT + P&L
        exit_price, exit_date, days, result = 0, None, 0, 'Running'
        for j in range(i + 1, len(df)):
            days += 1
            h, l, c = df['High'].iloc[j], df['Low'].iloc[j], df['Close'].iloc[j]

            if l <= sl:
                exit_price, exit_date, result = sl, df.index[j], 'SL Hit'
                break
            if h >= target:
                exit_price, exit_date, result = target, df.index[j], 'Target Hit'
                break
            if j == len(df) - 1:
                exit_price, exit_date, result = c, df.index[j], 'Running'

        if exit_date is None:
            continue

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        setups.append({
            'entry_date': curr.name.strftime('%Y-%m-%d'),
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'exit_date': exit_date.strftime('%Y-%m-%d'),
            'exit_price': round(exit_price, 2),
            'days': days,
            'pl_pct': round(pl_pct, 2),
            'result': result,
            'risk_pct': round(risk_pct, 1),
            'buyer_20d': round(buyer_20d/100000, 1),
            'seller_20d': round(seller_20d/100000, 1),
            'obv': round(curr['OBV'], 0),
            'obv_sma': round(curr['OBV_20SMA'], 0)
        })

    return setups

# 5. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 150:
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']

        if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
            print("Volume/Turnover kam")
            continue

        trades = find_obv_setup(df, ref_date)
        if len(trades) == 0:
            print("No OBV Setup")
            continue

        for trade in trades:
            rr = (trade['target'] - trade['entry_price']) / (trade['entry_price'] - trade['sl'])

            print(f" ✅ {trade['entry_date']} | {trade['result']} | P&L: {trade['pl_pct']}%")

            signals.append({
                'Stock': stock,
                'Entry_Date': trade['entry_date'],
                'Entry_Price': trade['entry_price'],
                'SL': trade['sl'],
                'Target': trade['target'],
                'Exit_Date': trade['exit_date'],
                'Exit_Price': trade['exit_price'],
                'Days': trade['days'],
                'P&L_%': trade['pl_pct'],
                'Result': trade['result'],
                'Risk_%': trade['risk_pct'],
                'R:R': round(rr, 1),
                'Buyer_20D_L': trade['buyer_20d'],
                'Seller_20D_L': trade['seller_20d'],
                'OBV': trade['obv'],
                'OBV_SMA': trade['obv_sma'],
                'AvgVol_Lakh': round(avg_vol/100000, 1)
            })

        if (i + 1) % 20 == 0:
            time.sleep(0.5)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE
try:
    ws_output = sh.worksheet("OBV_Setups")
except:
    ws_output = sh.add_worksheet(title="OBV_Setups", rows=1000, cols=18)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('P&L_%', ascending=False)
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    total_trades = len(df_out)
    wins = len(df_out[df_out['Result'] == 'Target Hit'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    total_pl = df_out['P&L_%'].astype(float).sum()

    summary = [
        ['', ''],
        ['TOTAL TRADES', total_trades],
        ['WINS', wins],
        ['WIN RATE %', win_rate],
        ['TOTAL P&L %', round(total_pl, 2)],
        ['AVG P&L %', round(total_pl / total_trades, 2) if total_trades > 0 else 0]
    ]
    ws_output.update(f'A{len(payload)+2}', summary)

    print(f"\n=== DONE: {len(signals)} OBV TRADES | WIN RATE: {win_rate}% | TOTAL P&L: {total_pl:.1f}% ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No OBV Setups Found"]])
    print("\n=== DONE: 0 SETUPS ===")
