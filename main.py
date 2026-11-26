import json
import os
import gspread
import pandas as pd

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
ws = sh.worksheet("M_SWING_RUN_CORRECTION_LOG")

rows = ws.get_all_values()
# Row 1 is banner title, Row 2 is column headers, Row 3+ is trade data
headers = rows[1]
data = rows[2:]

df = pd.DataFrame(data, columns=headers)
df["PnL %"] = pd.to_numeric(df["PnL %"], errors="coerce")
df = df.dropna(subset=["PnL %"])

summary = []
for sig, grp in df.groupby("Signal"):
  total = len(grp)
  wins = len(grp[grp["Result"] == "WIN"])
  losses = len(grp[grp["Result"] == "LOSS"])
  win_rate = round((wins / total) * 100, 2) if total > 0 else 0.0
  total_pnl = round(grp["PnL %"].sum(), 2)
  avg_pnl = round(grp["PnL %"].mean(), 2)
  summary.append({
      "Signal": sig,
      "Trades": total,
      "Wins": wins,
      "Losses": losses,
      "Win_Rate_%": win_rate,
      "Total_PnL_%": total_pnl,
      "Avg_PnL_%": avg_pnl,
  })

df_sum = pd.DataFrame(summary).sort_values(by="Total_PnL_%", ascending=False)
print("\n=== SIGNAL-WISE BREAKDOWN (M_SWING_RUN_CORRECTION_LOG) ===")
print(df_sum.to_string(index=False))

tab_out = "SIGNAL_BREAKDOWN"
ws_out = (
    sh.worksheet(tab_out)
    if tab_out in [x.title for x in sh.worksheets()]
    else sh.add_worksheet(tab_out, 50, 10)
)
ws_out.clear()
ws_out.append_row(list(df_sum.columns))
ws_out.append_rows(df_sum.values.tolist(), value_input_option="USER_ENTERED")
print(f"\n✅ Saved breakdown to '{tab_out}' tab!")
