import os
import datetime
import pandas as pd
import numpy as np
import requests
import gspread
import time
from google.oauth2.service_account import Credentials

# ==========================================
# 1. SETTINGS & CONFIGURATIONS
# ==========================================
Ticker_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
Backup_Ticker_URL = "https://raw.githubusercontent.com/datasets/top-100-companies-india/master/data/nifty_100.csv"

MAX_HOLD_DAYS = 30
COOLDOWN_DAYS = 10
TARGET_PCT = 0.10
STOP_LOSS_PCT = 0.05

Creds_Dict = {
    "type": os.environ.get("GCP_TYPE"),
    "project_id": os.environ.get("GCP_PROJECT_ID"),
    "private_key_id": os.environ.get("GCP_PRIVATE_KEY_ID"),
    "private_key": os.environ.get("GCP_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": os.environ.get("GCP_CLIENT_EMAIL"),
    "client_id": os.environ.get("GCP_CLIENT_ID"),
    "auth_uri": os.environ.get("GCP_AUTH_URI"),
    "token_uri": os.environ.get("GCP_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.environ.get("GCP_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.environ.get("GCP_CLIENT_X509_CERT_URL"),
    "universe_domain": os.environ.get("GCP_UNIVERSE_DOMAIN")
}

# ==========================================
# 2. FIXED YAHOO FINANCE SCRAPER WITH DELAY
# ==========================================
def fetch_yfinance_stealth(ticker, days=365):
    end_dt = datetime.datetime.now()
    start_dt = end_dt - datetime.timedelta(days=days)
    
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    
    url = f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    
    try:
        # Rate limit se bachne ke liye chhota sa delay
        time.sleep(0.1) 
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        
        lines = res.text.split('\n')
        if len(lines) <= 1:
            return None
            
        data = []
        for line in lines[1:]:
            row = line.strip().split(',')
            if len(row) == 7 and 'null' not in row:
                data.append(row)
                
        if not data:
            return None
            
        df = pd.DataFrame(data, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'])
        for col in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']:
            df[col] = pd.to_numeric(df[col])
            
        return df.sort_values('Date').reset_index(drop=True)
    except Exception:
        return None

# ==========================================
# 3. FIXED PRICE ACTION LOGIC
# ==========================================
def build_indicators(df):
    if len(df) < 21:
        return df
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5) # Prevent division by zero
    return df

def check_pure_price_action(df, idx):
    if idx < 1:
        return False, None
        
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    is_green = row['Close'] > row['Open']
    
    # STRATEGY 1: PA_SUPPORT_RETEST
    # Target criteria thoda flexible kiya taaki setups miss na ho (1.2% -> 2.0%)
    low_near_support = ((row['Low'] / row['Support_20D']) - 1) * 100 <= 2.0
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    strong_rejection = lower_wick >= (body * 1.0)
    
    if low_near_support and (strong_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # STRATEGY 2: PA_CHoCH_BREAKOUT (Bug Fixed Here)
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    
    # FIX: .get() hata kar direct column data access kiya
    strong_volume = row['Vol_Multiple'] > 1.25 
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, None

# ==========================================
# 4. BACKTESTING BACKBONE
# ==========================================
def run_backtest_for_ticker(ticker, df):
    if df is None or len(df) < 30:
        return []
        
    df = build_indicators(df)
    signals = []
    idx = 21
    total_rows = len(df)
    
    while idx < total_rows:
        is_signal, mode = check_pure_price_action(df, idx)
        if is_signal:
            entry_row = df.iloc[idx]
            entry_price = entry_row['Close']
            entry_date = entry_row['Date']
            
            target_price = entry_price * (1 + TARGET_PCT)
            sl_price = entry_price * (1 - STOP_LOSS_PCT)
            
            result = "TIMEOUT"
            exit_date = None
            exit_price = entry_price
            
            exit_idx = idx + 1
            while exit_idx < min(idx + 1 + MAX_HOLD_DAYS, total_rows):
                future_row = df.iloc[exit_idx]
                
                if future_row['High'] >= target_price:
                    result = "PROFIT"
                    exit_date = future_row['Date']
                    exit_price = target_price
                    break
                elif future_row['Low'] <= sl_price:
                    result = "LOSS"
                    exit_date = future_row['Date']
                    exit_price = sl_price
                    break
                    
                exit_price = future_row['Close']
                exit_date = future_row['Date']
                exit_idx += 1
                
            signals.append({
                "Ticker": ticker,
                "Strategy Mode": mode,
                "Entry Date": entry_date.strftime('%Y-%m-%d'),
                "Entry Price": round(entry_price, 2),
                "Exit Date": exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                "Exit Price": round(exit_price, 2),
                "Result": result
            })
            idx = exit_idx + COOLDOWN_DAYS
        else:
            idx += 1
            
    return signals

# ==========================================
# 5. MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("=== PURE PRICE ACTION RAW BACKTEST ENGINE V16 ===")
    
    ticker_list = []
    try:
        tickers_df = pd.read_csv(Ticker_URL)
        col_name = 'SYMBOL' if 'SYMBOL' in tickers_df.columns else 'Symbol'
        ticker_list = tickers_df[col_name].dropna().tolist()
    except Exception as e:
        print(f"Primary URL failed, loading backup...")
        try:
            tickers_df = pd.read_csv(Backup_Ticker_URL)
            col_name = 'Symbol' if 'Symbol' in tickers_df.columns else tickers_df.columns[0]
            ticker_list = tickers_df[col_name].dropna().tolist()
        except Exception:
            # Final safe list
            ticker_list = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "TATAMOTORS", "SBIN", "ITC"]
            
    ticker_list = [str(t).strip() + ".NS" for t in ticker_list if not str(t).strip().endswith(".NS")]
    
    # Sirf Top 200 stocks scan karenge taaki Yahoo block na kare aur genuine report mile
    ticker_list = ticker_list[:200]
    print(f"Scanning Top {len(ticker_list)} highly liquid NSE stocks to get clear win-rate metrics...")
    
    all_signals = []
    success_count = 0
    
    for ticker in ticker_list:
        df = fetch_yfinance_stealth(ticker, days=365)
        if df is not None:
            success_count += 1
            ticker_signals = run_backtest_for_ticker(ticker, df)
            all_signals.extend(ticker_signals)
            
    print(f"\nSuccessfully downloaded data for {success_count}/{len(ticker_list)} stocks.")
    
    if not all_signals:
        print("⚠️ No signals found even after code optimization. Adjusting logic might be required.")
        return
        
    final_df = pd.DataFrame(all_signals)
    
    # Summary Print
    print("\n=======================================================")
    print("📢 FIXED STRATEGY PERFORMANCE REPORT 📢")
    print("=======================================================")
    print(f"{'Strategy Mode':<20} | {'Total':<5} | {'Wins':<5} | {'Losses':<6} | {'Timeouts':<8} | {'Win Rate %'}")
    print("-" * 65)
    
    for m in ["PA_SUPPORT_RETEST", "PA_CHoCH_BREAKOUT"]:
        m_df = final_df[final_df["Strategy Mode"] == m]
        tot = len(m_df)
        if tot > 0:
            w = len(m_df[m_df["Result"] == "PROFIT"])
            l = len(m_df[m_df["Result"] == "LOSS"])
            t = len(m_df[m_df["Result"] == "TIMEOUT"])
            wr = round((w / tot) * 100, 2)
            print(f"{m:<20} | {tot:<5} | {w:<5} | {l:<6} | {t:<8} | {wr}%")
        else:
            print(f"{m:<20} | 0     | 0     | 0      | 0        | 0.0%")
    print("=======================================================\n")
    
    # Sheets Update
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(Creds_Dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open("CTD_Sniper_Scanner")
        wks = sheet.get_worksheet(0)
        
        final_df = final_df.fillna("") 
        wks.clear() 
        sheet_data = [final_df.columns.values.tolist()] + final_df.values.tolist()
        wks.update(sheet_data) 
        print("Google Sheets Updated Successfully!")
    except Exception as e:
        print(f"Sheets Error: {e}")

if __name__ == "__main__":
    main()
    
