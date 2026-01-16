import aiosqlite
import logging
import os
from datetime import datetime
import pytz
import asyncio

logger = logging.getLogger(__name__)

# Default DB Path (can be overridden by Env Var for Docker volumes)
DB_PATH = os.getenv('DB_PATH', 'bot.db')

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    async def initialize(self):
        """Creates tables if they don't exist and seeds default data."""
        logger.info(f"[DB] Initializing database at {self.db_path}...")
        async with aiosqlite.connect(self.db_path) as db:
            # 1. Admin Users (Auth)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. System Settings (Dynamic Config)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    type TEXT -- 'int', 'float', 'str'
                )
            """)

            # 3. Exit Tiers (Stepped TP)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS exit_tiers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    days_min INTEGER,
                    days_max INTEGER,
                    target_pnl FLOAT
                )
            """)

            # 4. Trades (History)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    contract_id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    entry_date TIMESTAMP,
                    entry_price FLOAT,
                    quantity INTEGER,
                    exit_date TIMESTAMP,
                    exit_price FLOAT,
                    pnl_raw FLOAT,
                    exit_reason TEXT,
                    status TEXT -- 'OPEN', 'CLOSED'
                )
            """)
            
            await db.commit()
            await self._seed_defaults(db)

    async def _seed_defaults(self, db):
        """Seeds default settings if they don't exist."""
        defaults = {
            'target_delta': ('0.6', 'float'),
            'entry_drop_pct': ('-0.01', 'float'),
            'min_expiry_days': ('365', 'int'),
            'max_positions': ('3', 'int'),
            'time_exit_days': ('270', 'int'),
            'roll_drop_pct': ('-0.05', 'float'),
            'ib_port': ('4004', 'int'), # Default port, can be changed via UI
            'leaps_realized_profit': ('0.0', 'float'),
            'qqqm_invested_capital': ('0.0', 'float'),
            'auto_invest_qqqm': ('0', 'int'),         # 0=off, 1=on
            'auto_invest_min_threshold': ('500', 'float') # Min profit to trigger buy
        }

        for key, (val, link_type) in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, type) VALUES (?, ?, ?)",
                (key, val, link_type)
            )
        
        # Seed Exit Tiers if empty
        cursor = await db.execute("SELECT COUNT(*) FROM exit_tiers")
        count = (await cursor.fetchone())[0]
        if count == 0:
            logger.info("[DB] Seeding default Exit Tiers...")
            tiers = [
                (0, 120, 0.50),
                (121, 180, 0.30),
                (181, 9999, 0.10) 
            ]
            await db.executemany(
                "INSERT INTO exit_tiers (days_min, days_max, target_pnl) VALUES (?, ?, ?)",
                tiers
            )
        
        await db.commit()

    # --- Core API ---

    async def get_setting(self, key, default=None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value, type FROM system_settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                if not row: return default
                
                val, type_str = row
                if type_str == 'int': return int(val)
                if type_str == 'float': return float(val)
                return val

    async def set_setting(self, key, value, type_str='str'):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, type) VALUES (?, ?, ?)",
                (key, str(value), type_str)
            )
            await db.commit()

    async def get_exit_tiers(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM exit_tiers ORDER BY days_min ASC") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def update_exit_tiers(self, tiers_list: list):
        """
        Bulk updates exit_tiers table.
        tiers_list: [{"days_min": 0, "days_max": 120, "target_pnl": 0.5}, ...]
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM exit_tiers")
            for tier in tiers_list:
                await db.execute(
                    "INSERT INTO exit_tiers (days_min, days_max, target_pnl) VALUES (?, ?, ?)",
                    (tier['days_min'], tier['days_max'], tier['target_pnl'])
                )
            await db.commit()

    async def get_open_positions(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM trades WHERE status = 'OPEN'") as cursor:
                 return [dict(row) for row in await cursor.fetchall()]

    async def save_trade(self, trade_data):
        """
        trade_data: dict with contract_id, symbol, entry_date, entry_price, quantity
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO trades 
                (contract_id, symbol, entry_date, entry_price, quantity, status)
                VALUES (?, ?, ?, ?, ?, 'OPEN')
            """, (
                trade_data['contract_id'], 
                trade_data['symbol'], 
                trade_data['entry_date'], 
                trade_data['entry_price'], 
                trade_data['quantity']
            ))
            await db.commit()

    async def close_trade(self, contract_id, exit_date, exit_price, reason):
        async with aiosqlite.connect(self.db_path) as db:
            # First get entry price to calc PnL
            async with db.execute("SELECT entry_price, quantity FROM trades WHERE contract_id = ?", (contract_id,)) as cursor:
                row = await cursor.fetchone()
                if not row: 
                    logger.error(f"Cannot close trade {contract_id}: Not found.")
                    return
                entry_price, quantity = row
                
                pnl_raw = (exit_price - entry_price) * quantity * 100 # Option multiplier usually 100

                await db.execute("""
                    UPDATE trades 
                    SET exit_date = ?, exit_price = ?, pnl_raw = ?, exit_reason = ?, status = 'CLOSED'
                    WHERE contract_id = ?
                """, (exit_date, exit_price, pnl_raw, reason, contract_id))
                await db.commit()

    async def has_traded_today(self, symbol, timezone):
        now = datetime.now(timezone)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Convert to naive or ISO if needed, depending on how we store dates.
        # SQLite stores timestamps as strings usually. We should ensure consistency.
        # Here we assume timestamps are stored as ISO strings.
        
        # NOTE: This implementation relies on string comparison of ISO dates which works for YYYY-MM-DD
        today_iso = today_start.isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
             async with db.execute(
                 "SELECT COUNT(*) FROM trades WHERE symbol = ? AND entry_date >= ?", 
                 (symbol, today_iso)
             ) as cursor:
                 count = (await cursor.fetchone())[0]
                 return count > 0

    # --- Auth & First Run ---
    
    async def get_admin_count(self):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM admin_users") as cursor:
                return (await cursor.fetchone())[0]

    async def create_admin(self, username, password_hash):
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", 
                    (username, password_hash)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def get_admin_password(self, username):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT password_hash FROM admin_users WHERE username = ?", (username,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
