import os, json, gspread, yfinance as yf, pandas as pd
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
print(f"=== MINERVINI 8/8 SOLID | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = list(set([(s.strip().upper()+".NS" if not s.strip().upper().endswith(".NS") else s.strip().upper()) for s in raw if s and s.upper() not in ["STOCK","SYMBOL"] and "ETF" not in s.upper() and "BEES" not in s.upper()]))
print(f"Stocks: {len(STOCKS)}")

# Saare stocks ka data pehle download - RS rank ke liye
all_data = {}
for sym in STOCKS:
    try:
        d = yf.Ticker(sym).history(period="1y", auto_adjust=True)
        if len(d)>200:
            if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
            all_data[sym]=d
    except: pass

print(f"Data loaded: {len(all_data)}")

all_trades=[]
for symbol, d in all_data.items():
    try:
        d['SMA50']=d['Close'].rolling(50).mean()
        d['SMA150']=d['Close'].rolling(150).mean()
        d['SMA200']=d['Close'].rolling(200).mean()
        d['Vol50']=d['Volume'].rolling(50).mean()
        d['52W_High']=d['High'].rolling(260).max()
        d['52W_Low']=d['Low'].rolling(260).min()
        d['Ret120']=d['Close'].pct_change(120)*100 # 6 month return for RS

        for i in range(200, len(d)-20):
            row=d.iloc[i]
            if pd.isna(row['SMA200']) or pd.isna(row['Ret120']): continue
            
            # --- MINERVINI 8 RULES ---
            cond1 = row['Close'] > row['SMA150'] and row['Close'] > row['SMA200']
            cond2 = row['SMA150'] > row['SMA200']
            cond3 = row['SMA200'] > d['SMA200'].iloc[i-20]
            cond4 = row['SMA50'] > row['SMA150'] and row['SMA50'] > row['SMA200']
            cond5 = row['Close'] > row['SMA50']
            cond6 = row['Close'] >= row['52W_High']*0.75
            cond7 = row['Close'] >= row['52W_Low']*1.30
            
            # RULE 8 - RS RANK 70+
            # Is date pe sab stocks ka Ret120 nikal ke rank
            curr_date = d.index[i]
            all_rets=[]
            for s2, d2 in all_data.items():
                try:
                    # same date ke aas paas ka return
                    idx = d2.index.get_indexer([curr_date], method='nearest')[0]
                    if idx>=120:
                        ret = (d2['Close'].iloc[idx]/d2['Close'].iloc[idx-120]-1)*100
                        all_rets.append(ret)
                except: pass
            
            if len(all_rets)<20: continue
            all_rets_sorted = sorted(all_rets)
            # current stock ka percentile
            rank_pct = (all_rets_sorted.index(row['Ret120']) / len(all_rets_sorted))*100 if row['Ret120'] in all_rets_sorted else 50
            # approx rank
            import bisect
            rank_pct = bisect.bisect_left(all_rets_sorted, row['Ret120']) / len(all_rets_sorted) * 100
            cond8 = rank_pct >= 70
            
            if not all([cond1,cond2,cond3,cond4,cond5,cond6,cond7,cond8]): continue

            # --- DARVAS BOX ---
            box = d.iloc[i-15:i]
            box_high = box['High'].max()
            box_low = box['Low'].min()
            box_range = (box_high - box_low)/box_low*100
            if not (3 <= box_range <= 12): continue
            
            if not (row['Close'] > box_high and row['Volume'] > row['Vol50']*1.5): continue

            entry=row['Close']; sl=box_low; risk=(entry-sl)/entry*100
            if not (2 <= risk <= 7): continue

            future=d.iloc[i+1:i+21]
            hit5=False
            for _, r in future.iterrows():
                if r['Low'] <= sl: break
                if r['High'] >= entry*1.05: hit5=True; break
            
            all_trades.append([symbol.replace(".NS",""), d.index[i].strftime('%Y-%m-%d'), round(entry,2), round(sl,2), round(risk,2), round(rank_pct,0), hit5])
    except Exception as e:
        print(f"{symbol} err {e}")
        continue

ws = sh.worksheet("Backtest_Solid_RS") if "Backtest_Solid_RS" in [x.title for x in sh.worksheets()] else sh.add_worksheet("Backtest_Solid_RS",5000,10)
ws.clear()
if all_trades:
    df=pd.DataFrame(all_trades, columns=["Stock","Date","Entry","SL","Risk%","RS%","Hit_5%"])
    win=df['Hit_5%'].sum()/len(df)*100
    print(f"TOTAL {len(df)} | 5% WIN {win:.1f}% | 8/8")
    ws.append_row([f"Total {len(df)} | 5% Win {win:.1f}% | Minervini 8/8 + Darvas"])
    ws.append_row(["Stock","Date","Entry","SL","Risk%","RS%","Hit_5%"])
    ws.append_rows(df.values.tolist())
else:
    print("No trades - 8/8 bahut strict hai, yehi solid ka matlab")
