
import persistence
import config
from datetime import datetime

def generate_daily_report():
    """
    Generates a text summary of:
    1. Active LEAPS (with days held).
    2. Today's Trades.
    3. Estimated PnL from closed trades.
    """
    lines = []
    now = datetime.now(config.TIMEZONE)
    today_str = now.date().isoformat()
    
    lines.append(f"=== QQQ LEAPS Bot Report: {today_str} ===")
    
    # 1. Closed Trades PnL
    total_pnl = 0.0
    closed_count = 0
    today_trades = []
    
    # We load all to scan (inefficient for years of data, fine for personal bot)
    # Better: persistence should have specific query methods, but this is V1.
    import csv
    import os
    
    if os.path.exists(persistence.CSV_FILE):
        with open(persistence.CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Track PnL
                if row['Status'] == 'CLOSED':
                    try:
                        total_pnl += float(row['PnL'])
                        closed_count += 1
                    except: pass
                
                # Track Today's Activity
                if row['EntryDate'].startswith(today_str) or (row['ExitDate'] and row['ExitDate'].startswith(today_str)):
                    today_trades.append(row)

    lines.append(f"Total Realized PnL (All Time): ${total_pnl:.2f}")
    lines.append(f"Total Closed Trades: {closed_count}")
    
    # 2. Active Positions
    open_positions = persistence.load_open_positions()
    lines.append(f"\n--- Open Positions ({len(open_positions)}) ---")
    for pos in open_positions:
        entry_date = pos['EntryDate']
        try:
            # simple parse
            ed = datetime.fromisoformat(entry_date)
            # make offset aware if needed, assuming stored as iso
            if ed.tzinfo is None:
                 ed = config.TIMEZONE.localize(ed)
            days = (now - ed).days
        except:
            days = "?"
            
        lines.append(f"• {pos['Symbol']} (ID: {pos['ContractID']}) | Qty: {pos['Quantity']} | Entry: ${pos['EntryPrice']} | Held: {days} days")

    # 3. Today's Trades
    lines.append(f"\n--- Today's Activity ({len(today_trades)}) ---")
    if not today_trades:
        lines.append("No trades executed today.")
    else:
        for t in today_trades:
            status = "OPENED" if t['Status'] == 'OPEN' else "CLOSED"
            if t['ExitDate'].startswith(today_str) and t['Status'] == 'CLOSED':
                lines.append(f"• SOLD {t['Symbol']} (ID: {t['ContractID']}) @ ${t['ExitPrice']} | PnL: ${t['PnL']} | Reason: {t['ExitSignal']}")
            elif t['EntryDate'].startswith(today_str):
                lines.append(f"• BOUGHT {t['Symbol']} (ID: {t['ContractID']}) @ ${t['EntryPrice']}")

    report = "\n".join(lines)
    print(report)
    return report

if __name__ == "__main__":
    generate_daily_report()
