import os, json, math, gspread, yfinance as yf, pandas as pd
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
print(f"=== SOLID 10W BASE | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = list(set([s.strip().upper()+".NS" if not s.strip().upper().endswith(".NS") else s.strip().upper() for s in raw if s and s.upper() not in ["STOCK","SYMBOL"]]))

all_trades=[]
for symbol in STOCKS:
  try:
    stock_clean=symbol.replace(".NS","")
    d = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
    if len(d)<200: continue
    w = d.resample('W-FRI').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
    if len(w)<30: continue
    for i in range(20, len(w)-1):
        base = w.iloc[i-10:i]
        base_range = (base['High'].max() - base['Low'].min()) / base['Low'].min() * 100
        if base_range > 15: continue
        vol_base = base['Volume'].mean()
        vol_prev = w.iloc[i-20:i-10]['Volume'].mean()
        if vol_base > vol_prev*0.7: continue # volume sukha nahi
        curr = w.iloc[i]
        if not (curr['Close'] > base['High'].max() and curr['Volume'] >= 2.5*vol_base): continue
        
        entry=curr['Close']; sl=base['Low'].min(); risk=(entry-sl)/entry*100
        if risk<4 or risk>8: continue
        # 30 din forward
        future = d.loc[w.index[i]:].iloc[1:31]
        hit10 = (future['High'] >= entry*1.10).any()
        hitSL = False
        # SL pehle ya 10% pehle
        for _, r in future.iterrows():
            if r['Low'] <= sl: hitSL=True; break
            if r['High'] >= entry*1.10: break
        all_trades.append([stock_clean, w.index[i].strftime('%Y-%m-%d'), round(entry,2), round(sl,2), round(risk,2), hit10, hitSL])
  except: continue

ws = sh.worksheet("Backtest_Solid_10W") if "Backtest_Solid_10W" in [x.title for x in sh.worksheets()] else sh.add_worksheet("Backtest_Solid_10W",1000,10)
ws.clear()
if all_trades:
    df=pd.DataFrame(all_trades, columns=["Stock","Date","Entry","SL","Risk%","Hit_10%","SL_First"])
    print(f"Total {len(df)} | 10% Win {(df['Hit_10%'].sum()/len(df)*100):.1f}%")
    ws.append_row(["Stock","Date","Entry","SL","Risk%","Hit_10%","SL_First"])
    ws.append_rows(df.values.tolist())
else:
    ws.append_row(["No Trades - This is SOLID, kam hi milega"])
    print("No trades - filter solid hai")
