
import pytz

# --- Connection Settings ---
import os

# --- Connection Settings ---
IB_HOST = os.getenv('IB_HOST', '127.0.0.1')
IB_PORT = int(os.getenv('IB_PORT', 7497))  # Paper Trading Port
CLIENT_ID = int(os.getenv('CLIENT_ID', 1))   # Unique ID for this bot
RECONNECT_DELAY = 10 # Seconds to wait before reconnecting

# --- Strategy Parameters ---
SYMBOL = 'QQQ'
CORE_SYMBOL = 'QQQM' # Not currently used in logic but mentioned in spec
TIMEZONE = pytz.timezone('US/Eastern')

# Entry Rules
ENTRY_DROP_PCT = -0.01      # -1% drop from PrevClose
SCAN_INTERVAL_MIN = 5       # Check every 5 minutes

# Contract Selection (LEAPS)
MIN_EXPIRY_DAYS = 365
TARGET_DELTA = 0.6
DELTA_TOLERANCE = 0.1       # Allow 0.5 to 0.7 range if needed

# --- Risk Management (The Shield) ---
MAX_POSITIONS = 3

# Stepped Take Profit (TP) Logic
# 0-4 months (0-120 days): Target 50%
# 4-6 months (120-180 days): Target 30%
# 7-9 months (180-270 days): Target 10%
TP_TIERS = [
    (120, 0.50), # <= 120 days: 50%
    (180, 0.30), # <= 180 days: 30%
    (270, 0.10)  # <= 270 days: 10%
]

TIME_EXIT_DAYS = 270        # Sell if held >= 270 days (Force Exit)

# --- Execution Safety ---
MAX_SPREAD_RATIO = 0.01     # (Ask - Bid) / Mid <= 1%
MAX_PREMIUM = 12000.0       # Max $ per contract
ORDER_TIMEOUT = 30          # Seconds to wait for order fill before check
