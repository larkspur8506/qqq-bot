
import csv
import os
from datetime import datetime
import config

CSV_FILE = config.TRADES_CSV_PATH
HEADERS = [
    'ContractID', 'Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 
    'ExitSignal', 'ExitDate', 'ExitPrice', 'PnL', 'Status'
]

def init_db():
    """Initialize the CSV file with headers if it doesn't exist."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
        print(f"[Persistence] Created new {CSV_FILE}")
    else:
        print(f"[Persistence] Found existing {CSV_FILE}")

def save_trade(contract_id, symbol, entry_date, entry_price, quantity):
    """Log a new trade entry."""
    # Ensure entry_date is string format if passed as datetime
    if isinstance(entry_date, datetime):
        entry_date = entry_date.isoformat()

    row = {
        'ContractID': contract_id,
        'Symbol': symbol,
        'EntryDate': entry_date,
        'EntryPrice': f"{entry_price:.2f}",
        'Quantity': quantity,
        'ExitSignal': '',
        'ExitDate': '',
        'ExitPrice': '0.0',
        'PnL': '0.0',
        'Status': 'OPEN'
    }
    
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writerow(row)
    print(f"[Persistence] Saved NEW trade: {symbol} (ID: {contract_id})")

def load_open_positions():
    """
    Returns a list of dictionaries for all trades with Status='OPEN'.
    Used for state recovery on restart.
    """
    if not os.path.exists(CSV_FILE):
        return []

    open_positions = []
    with open(CSV_FILE, mode='r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['Status'] == 'OPEN':
                open_positions.append(row)
    return open_positions

def update_trade_exit(contract_id, exit_signal, exit_date, exit_price, pnl):
    """
    Updates an existing OPEN trade to CLOSED.
    Since CSV doesn't support direct update, we rewrite the file.
    This is acceptable for low-volume trading (max 3 positions).
    """
    if isinstance(exit_date, datetime):
        exit_date = exit_date.isoformat()

    rows = []
    updated = False
    
    # Read all rows
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='r', newline='') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    # Modify the target row
    for row in rows:
        if row['ContractID'] == str(contract_id) and row['Status'] == 'OPEN':
            row['ExitSignal'] = exit_signal
            row['ExitDate'] = exit_date
            row['ExitPrice'] = f"{exit_price:.2f}"
            row['PnL'] = f"{pnl:.2f}"
            row['Status'] = 'CLOSED'
            updated = True
            print(f"[Persistence] Updated trade EXIT: {contract_id} via {exit_signal}")
            break
    
    if updated:
        with open(CSV_FILE, mode='w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        print(f"[Persistence] WARNING: Could not find OPEN trade for ID {contract_id} to update.")

def has_traded_today(symbol, timezone_obj):
    """
    Checks if a trade was already entered today for the given symbol.
    Prevents multiple buys strictly based on Calendar Date (US/Eastern).
    """
    if not os.path.exists(CSV_FILE):
        return False
        
    today_str = datetime.now(timezone_obj).date().isoformat()
    
    with open(CSV_FILE, mode='r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # EntryDate format: YYYY-MM-DDTHH:MM:SS or similar
            # We compare the substring YYYY-MM-DD
            if row['Symbol'] == symbol and row['EntryDate'].startswith(today_str):
                return True
    return False

if __name__ == '__main__':
    # Simple test if run directly
    init_db()
    # Test Entry
    # save_trade(12345, 'QQQ_TEST', datetime.now(), 10.50, 1)
    # print("Open:", load_open_positions())
    # Test Exit
    # update_trade_exit(12345, 'TP', datetime.now(), 15.75, 525.0)
