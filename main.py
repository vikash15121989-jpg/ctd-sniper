import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== VSA SNIPER V2.1: ENTRY + EXIT + P&L TRACKER ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE HANDLING
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

# 3. OBV + BUYER/SELLER CALCULATOR - TERA LOGIC ✅
def calculate_obv_buyer_seller(df):
    df = df.copy()
    df['OBV'] = 0
    df['Buyer_Vol'] = 0
    df['Seller_Vol'] = 0

    for i in range(1, len(df)):
        # Normal OBV
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1] + df['Volume'].iloc[i]
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1] - df['Volume'].iloc[i]
        else:
            df.loc[df.index[i], 'OBV'] = df['OBV'].iloc[i-1]

        # TERA BUYER/SELLER LOGIC ✅
        h, l, c, v = df['High'].iloc[i], df['Low'].iloc[i], df['Close'].iloc[i], df['Volume'].iloc[i]
        range_hl = h - l
        if range_hl == 0:
            buyer, seller = 0, 0
        else:
            buyer = (c - l) / range_hl * v
            seller = (h - c) / range_hl * v

        df.loc[df.index[i], 'Buyer_Vol'] = buyer
        df.loc[df.index[i], 'Seller_Vol'] = seller

    df['20DMA'] = df['Close'].rolling(20).mean()
    df['OBV_20SMA'] = df['OBV'].rolling(20).mean()
    return df

# 4. BACKTEST ENGINE - EXIT + P&L NIKALO ✅
def backtest_trade(df, entry_idx, entry_price, sl, target):
    exit_price = 0
    exit_date = None
    days = 0
    result_type = 'Running'

    for j in range(entry_idx + 1, len(df)):
        days += 1
        h, l, c = df['High'].iloc[j], df['Low'].iloc[j], df['Close'].iloc[j]
        date = df.index[j]

        # SL pehle check karo
        if l <= sl:
            exit_price = sl
            exit_date = date
            result_type = 'SL Hit'
            break
        # Phir Target
        if h >= target:
            exit_price = target
            exit_date = date
            result_type = 'Target Hit'
            break
        # Last din
        if j == len(df) - 1:
            exit_price = c
            exit_date = date
            result_type = 'Running'

    if exit_date is None:
        return None

    pl_pct = ((exit_price - entry_price) / entry_price) * 100
    pl_rs = exit_price - entry_price

    return {
        'exit_date': exit_date.strftime('%Y-%m-%d'),
        'exit_price': round(exit_price, 2),
        'days': days,
        'pl_pct': round(pl_pct, 2),
        'pl_rs': round(pl_rs, 2),
        'result': result_type
    }

# 5. VSA SPRING HUNTER V2.1
def find_vsa_spring(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 25:
        return []

    df_till_date = calculate_obv_buyer_seller(df_till_date)
    trades = []

    for i in range(20, len(df_till_date) - 4):
        curr = df_till_date.iloc[i]
        prev = df_till_date.iloc[i-1]

        # CONDITION-1: SPRING - Seller > 3x Buyer + -8% drop
        prev_range = prev['High'] - prev['Low']
        if prev_range == 0: continue

        prev_buyer = prev['Buyer_Vol']
        prev_seller = prev['Seller_Vol']
        if prev_buyer == 0: continue

        spring_ratio = prev_seller / prev_buyer
        spring_drop = (prev['Low'] - prev['Open']) / prev['Open']

        if not (spring_ratio > 3 and spring_drop < -0.08):
            continue

        # CONDITION-2: CONFIRM - Buyer > 3x Seller in next 3 days
        confirm = False
        confirm_idx = None
        for j in range(1, 4):
            if i + j >= len(df_till_date): break
            conf_candle = df_till_date.iloc[i + j]
            if conf_candle['Seller_Vol'] == 0: continue
            if conf_candle['Buyer_Vol'] / conf_candle['Seller_Vol'] > 3:
                confirm = True
                confirm_idx = i + j
                break

        if not confirm:
            continue

        # CONDITION-3: 20 DMA ke upar
        if pd.isna(curr['20DMA']) or curr['Close'] <= curr['20DMA']:
            continue

        # CONDITION-4: OBV > OBV_20SMA + Rising
        if pd.isna(curr['OBV_20SMA']) or curr['OBV'] <= curr['OBV_20SMA'] or curr['OBV'] <= prev['OBV']:
            continue

        entry_price = curr['Close']
        sl = prev['Low'] * 0.98
        risk = entry_price - sl
        target = entry_price + (risk * 3)
        risk_pct = (risk / entry_price) * 100

        if risk_pct > 10:
            continue

        # BACKTEST KARO - EXIT + P&L NIKALO ✅
        backtest_result = backtest_trade(df, i, entry_price, sl, target)
        if backtest_result is None:
            continue

        # QUALITY GRADE
        if spring_ratio > 5:
            quality = "A++"
        elif spring_ratio > 4:
            quality = "A+"
        elif spring_ratio > 3:
            quality = "A"
        else:
            quality = "B"

        trades.append({
            'entry_date': curr.name.strftime('%Y-%m-%d'),
            'spring_date': prev.name.strftime('%Y-%m-%d'),
            'confirm_date': df_till_date.iloc[confirm_idx].name.strftime('%Y-%m-%d'),
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'risk_pct': round(risk_pct, 1),
            'quality': quality,
            'spring_ratio': round(spring_ratio, 1),
            'spring_drop': round(spring_drop * 100, 1),
            'obv': round(curr['OBV'], 0),
            'obv_sma': round(curr['OBV_20SMA'], 0),
            **backtest_result # EXIT + P&L ADD KAR DIYA
        })

    return trades

# 6. MAIN LOOP
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
            print("Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']

        if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
            print("Volume/Turnover kam")
            continue

        trades = find_vsa_spring(df, ref_date)
        if len(trades) == 0:
            print("No VSA Spring")
            continue

        for trade in trades:
            rr = (trade['target'] - trade['entry_price']) / (trade['entry_price'] - trade['sl'])

            print(f" ✅ {trade['quality']} | {trade['entry_date']} | {trade['result']} | P&L: {trade['pl_pct']}%")

            signals.append({
                'Stock': stock,
                'Quality': trade['quality'],
                'Entry_Date': trade['entry_date'],
                'Spring_Date': trade['spring_date'],
                'Confirm_Date': trade['confirm_date'],
                'Entry_Price': trade['entry_price'],
                'SL': trade['sl'],
                'Target': trade['target'],
                'Exit_Date': trade['exit_date'],
                'Exit_Price': trade['exit_price'],
                'Days_Held': trade['days'],
                'P&L_%': trade['pl_pct'],
                'P&L_Rs': trade['pl_rs'],
                'Result': trade['result'],
                'Risk_%': trade['risk_pct'],
                'R:R': round(rr, 1),
                'Spring_Ratio': trade['spring_ratio'],
                'Spring_Drop_%': trade['spring_drop'],
                'OBV': trade['obv'],
                'OBV_20SMA': trade['obv_sma'],
                'AvgVol_Lakh': round(avg_vol/100000, 1)
            })

        if (i + 1) % 20 == 0:
            time.sleep(0.5)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. SHEET UPDATE
try:
    ws_output = sh.worksheet("VSA_Backtest")
except:
    ws_output = sh.add_worksheet(title="VSA_Backtest", rows=1000, cols=22)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values(['Quality', 'P&L_%'], ascending=[False, False])
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    # SUMMARY ADD KARO
    total_trades = len(df_out)
    wins = len(df_out[df_out['Result'] == 'Target Hit'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    total_pl = df_out['P&L_%'].astype(float).sum()
    avg_pl = round(total_pl / total_trades, 2) if total_trades > 0 else 0

    summary = [
        ['', ''],
        ['TOTAL TRADES', total_trades],
        ['WINS', wins],
        ['WIN RATE %', win_rate],
        ['TOTAL P&L %', round(total_pl, 2)],
        ['AVG P&L %', avg_pl]
    ]
    ws_output.update(f'A{len(payload)+2}', summary)

    print(f"\n=== DONE: {len(signals)} TRADES | WIN RATE: {win_rate}% | TOTAL P&L: {total_pl:.1f}% ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No VSA Springs Found"]])
    print("\n=== DONE: 0 SETUPS ===")
