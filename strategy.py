
import asyncio
from datetime import datetime, timedelta
import logging
from ib_insync import *
import config
import persistence
import execution

logger = logging.getLogger(__name__)

from persistence import Database

class Strategy:
    def __init__(self, ib: IB, database: Database):
        self.ib = ib
        self.db = database
        self.qqq_contract = Stock(config.SYMBOL, 'SMART', 'USD')
        self.prev_close = None
        self.last_day_check = None 
        
        # Cache settings
        self.settings = {} 

    async def initialize(self):
        """
        Run startup tasks:
        1. Load settings from DB.
        2. Set Delayed Market Data (Type 3) if no subscription.
        3. Qualify QQQ contract.
        4. Fetch authoritative PrevClose (Daily Bar).
        Returns: True if successful, False otherwise.
        """
        logger.info("[Strategy] Initializing...")
        
        # Load Settings immediately so Web UI has data
        await self.load_settings()
        
        # Switch to Delayed Market Data (3) as requested by user
        self.ib.reqMarketDataType(3) 
        try:
            await self.ib.qualifyContractsAsync(self.qqq_contract)
            
            # Helper to request historical data
            async def get_bars(what):
                return await self.ib.reqHistoricalDataAsync(
                    self.qqq_contract, endDateTime='', durationStr='2 D', 
                    barSizeSetting='1 day', whatToShow=what, useRTH=True
                )

            # Try TRADES first, then MIDPOINT
            bars_2d = await get_bars('TRADES')
            if not bars_2d:
                logger.info("[Strategy] TRADES data unavailable, trying MIDPOINT...")
                bars_2d = await get_bars('MIDPOINT')

            if bars_2d:
                today_str = datetime.now(config.TIMEZONE).date().isoformat()
                found_prev = False
                for bar in reversed(bars_2d):
                    bar_date_str = bar.date.isoformat() if hasattr(bar.date, 'isoformat') else str(bar.date)
                    if bar_date_str < today_str:
                        self.prev_close = bar.close
                        logger.info(f"[Strategy] PrevClose established from {bar_date_str}: {self.prev_close}")
                        found_prev = True
                        break
                
                if not found_prev:
                    self.prev_close = bars_2d[0].close
                    logger.warning(f"[Strategy] Using first available bar close: {self.prev_close}")
            
            # Last Resort: If history query failed, use live ticker data
            if self.prev_close is None:
                logger.warning("[Strategy] History query failed. Attempting to get PrevClose from Snapshot...")
                ticker = self.ib.reqMktData(self.qqq_contract, '', True, False)
                await asyncio.sleep(2)
                if ticker.close and ticker.close > 0:
                    self.prev_close = ticker.close
                    logger.info(f"[Strategy] PrevClose established from Ticker Snapshot: {self.prev_close}")
        
        except Exception as e:
            logger.error(f"[Strategy] Error during initialization: {e}")
            return False

        if self.prev_close is None:
            logger.error("[Strategy] CRITICAL: Could not fetch PrevClose. Bot cannot trade safely.")
            return False
        
        return True

    async def load_settings(self):
        """Helper to load all settings from DB into memory cache"""
        self.settings['entry_drop_pct'] = await self.db.get_setting('entry_drop_pct', -0.01)
        self.settings['target_delta'] = await self.db.get_setting('target_delta', 0.6)
        self.settings['min_expiry_days'] = await self.db.get_setting('min_expiry_days', 365)
        self.settings['max_positions'] = await self.db.get_setting('max_positions', 3)
        self.settings['time_exit_days'] = await self.db.get_setting('time_exit_days', 270)
        self.settings['delta_tolerance'] = await self.db.get_setting('delta_tolerance', 0.05)
        self.settings['max_option_spread'] = await self.db.get_setting('max_option_spread', 0.03)
        self.settings['roll_drop_pct'] = await self.db.get_setting('roll_drop_pct', -0.05)
        self.settings['leaps_realized_profit'] = await self.db.get_setting('leaps_realized_profit', 0.0)
        self.settings['qqqm_invested_capital'] = await self.db.get_setting('qqqm_invested_capital', 0.0)
        self.settings['auto_invest_qqqm'] = await self.db.get_setting('auto_invest_qqqm', 0)
        self.settings['auto_invest_min_threshold'] = await self.db.get_setting('auto_invest_min_threshold', 500.0)
        self.settings['order_quantity'] = await self.db.get_setting('order_quantity', 1)
    async def run_cycle(self):
        """
        The main logic executed every 5 minutes.
        """
        # Reload Settings from DB
        await self.load_settings()
        if self.prev_close is None:
            await self.initialize() # Retry init if failed previously
            if self.prev_close is None: 
                return

        # 1. Update Market Price
        # Ensure Delayed Data (3) is active (in case find_leaps changed it to Frozen/4)
        self.ib.reqMarketDataType(3) 
        ticker = self.ib.reqMktData(self.qqq_contract, '', True, False)
        while ticker.last != ticker.last: # Nan check
            await asyncio.sleep(0.1)
        
        current_price = ticker.last if ticker.last > 0 else (ticker.close if ticker.close > 0 else self.prev_close)
        
        # Calculate Drop
        pct_change = (current_price - self.prev_close) / self.prev_close
        logger.info(f"[Scan] QQQ: ${current_price:.2f} | PrevClose: ${self.prev_close:.2f} | Change: {pct_change:.2%}")

        # 2. Check Exits (The Shield)
        await self.manage_positions(current_price)

        # 3. Check Signal (The Spear)
        entry_drop_trigger = self.settings['entry_drop_pct']
        roll_drop_trigger = self.settings['roll_drop_pct']
        
        open_positions = await self.db.get_open_positions()
        max_positions = self.settings['max_positions']

        if pct_change <= roll_drop_trigger and len(open_positions) >= max_positions:
            logger.info(f"[ROLL] TRIGGERED: {pct_change:.2%} <= {roll_drop_trigger:.2%} and at MAX positions.")
            await self.process_roll_signal()
        elif pct_change <= entry_drop_trigger:
            logger.info(f"[Signal] DROP DETECTED: {pct_change:.2%} <= {entry_drop_trigger:.2%}")
            order_qty = self.settings.get('order_quantity', 1)
            await self.process_entry_signal(quantity=order_qty)
        else:
            logger.info("[Signal] No entry signal.")

    async def get_all_holdings(self):
        """
        Fetches all current positions from IBKR and categorizes them.
        """
        if not self.ib.isConnected():
            return None

        # 1. Account Summary (Cash, NetLiquidity)
        acc_values = self.ib.accountValues()
        summary = {
            "NetLiquidity": 0.0,
            "TotalCashValue": 0.0,
            "BuyingPower": 0.0,
            "Currency": "USD"
        }
        
        cashes_to_show = []
        seen_currencies = set()

        for v in acc_values:
            # Check for multiple variations of Net Liquidity tags
            if v.tag in ['NetLiquidity', 'NetLiquidation', 'EquityWithLoanValue', 'NetLiquidationByCurrency']:
                try:
                    val = float(v.value)
                    # Prefer BASE or USD, but take anything if currently 0
                    if val != 0 and (v.currency in ['BASE', 'USD'] or summary['NetLiquidity'] == 0):
                        summary['NetLiquidity'] = val
                except: pass
            
            if v.tag in ['TotalCashValue', 'CashBalance', 'TotalCashBalance']:
                try:
                    val = float(v.value)
                    if val != 0 and (v.currency in ['BASE', 'USD'] or summary['TotalCashValue'] == 0):
                        summary['TotalCashValue'] = val
                    
                    # Also collect individual currency balances for the holdings table (deduplicated)
                    if v.currency != 'BASE' and val != 0 and v.currency not in seen_currencies:
                        seen_currencies.add(v.currency)
                        cashes_to_show.append({
                            "conId": 0,
                            "symbol": f"CASH ({v.currency})",
                            "secType": "CASH",
                            "quantity": 1,
                            "mktPrice": val,
                            "mktValue": val,
                            "unrealizedPnL": 0.0,
                            "type": "CASH"
                        })
                except: pass

            if v.tag == 'BuyingPower': 
                try:
                    summary['BuyingPower'] = float(v.value)
                except: pass

        # 2. Positions
        positions = self.ib.positions()
        holdings = cashes_to_show # Start with cash items
        
        # Enrich with DB data for tracked LEAPS
        db_positions = await self.db.get_open_positions()
        db_map = {p['contract_id']: p for p in db_positions}

        for pos in positions:
            contract = pos.contract
            item = {
                "conId": contract.conId,
                "symbol": contract.localSymbol or contract.symbol,
                "secType": contract.secType,
                "quantity": pos.position,
                "mktPrice": pos.avgCost, # Default fallback
                "mktValue": 0.0,
                "unrealizedPnL": 0.0,
                "type": "OTHER"
            }

            # Categorization
            if contract.secType == 'STK':
                item['type'] = 'STOCK'
            elif contract.secType == 'OPT':
                item['type'] = 'OPTION'
            elif contract.secType in ['BOND', 'BILL']:
                item['type'] = 'BOND'
            elif contract.secType in ['CASH', 'FX']:
                item['type'] = 'CASH'

            # Request market data for price/value (simplified)
            # In a production bot, we'd use a ticker cache
            ticker = self.ib.ticker(contract)
            if ticker:
                item['mktPrice'] = ticker.marketPrice()
                item['mktValue'] = item['mktPrice'] * pos.position * (100 if contract.secType == 'OPT' else 1)
                item['unrealizedPnL'] = (item['mktPrice'] - pos.avgCost) * pos.position * (100 if contract.secType == 'OPT' else 1)

            # Match with DB
            if contract.conId in db_map:
                db_info = db_map[contract.conId]
                item['entry_date'] = db_info['entry_date']
                item['entry_price'] = db_info['entry_price']
                item['is_tracked'] = True

            holdings.append(item)

        return {
            "summary": summary,
            "holdings": holdings
        }

    async def process_roll_signal(self):
        """
        ROLL logic: Sell oldest option, buy replacement at lower strike.
        """
        open_positions = await self.db.get_open_positions()
        if not open_positions:
            return

        # FIFO: Oldest is first in list (usually ordered by entry_date)
        # Ensure they are sorted by date
        sorted_pos = sorted(open_positions, key=lambda x: str(x['entry_date']))
        oldest_pos = sorted_pos[0]
        
        contract_id = int(oldest_pos['contract_id'])
        quantity = int(oldest_pos['quantity'])
        logger.info(f"[ROLL] Replacing oldest position: ContractID {contract_id}")

        # 1. Sell Oldest
        contract = Contract(conId=contract_id, exchange='SMART')
        await self.ib.qualifyContractsAsync(contract)
        mid, _ = await execution.get_mid_price(self.ib, contract)
        
        if mid:
            trade = await execution.place_limit_order(self.ib, contract, 'SELL', quantity, mid)
            if trade and trade.orderStatus.status == 'Filled':
                 now = datetime.now(config.TIMEZONE)
                 await self.db.close_trade(contract_id, now, trade.orderStatus.avgFillPrice, 'ROLL_EXIT')
                 logger.info(f"[ROLL] SELL SUCCESS for {contract.localSymbol}")
                 
                 # Record Profit
                 await self.record_realized_profit(contract_id, float(oldest_pos['entry_price']), trade.orderStatus.avgFillPrice, quantity, trade.fills)
                 
                 # 2. Buy Replacement
                 # We trigger a normal entry search which will pick current best LEAP (lower strike now)
                 # Force=True to bypass one-trade-per-day check, and pass the original quantity
                 await self.process_entry_signal(force=True, quantity=quantity)
                 
                 # Check for QQQM Auto-Invest
                 await self.check_and_invest_profits()
            else:
                 logger.warning("[ROLL] Sell order failed to fill. Aborting roll.")
        else:
            logger.warning("[ROLL] Could not get price for sell order.")

    async def manage_positions(self, current_underlying_price):
        """
        Checks TP and Time Exit for all open positions.
        """
        open_positions = await self.db.get_open_positions()
        if not open_positions:
            return

        logger.info(f"[Manager] Checking {len(open_positions)} open positions...")
        
        now = datetime.now(config.TIMEZONE)
        
        # Load Exit Tiers from DB
        tiers = await self.db.get_exit_tiers()

        for pos in open_positions:
            contract_id = int(pos['contract_id'])
            entry_price = float(pos['entry_price'])
            # Ensure proper datetime parsing from SQLite (ISO format usually)
            entry_date_str = pos['entry_date']
            try:
                # SQLite might store as "YYYY-MM-DD HH:MM:SS.ssssss" or similar
                # We need to ensure we can parse it.
                if isinstance(entry_date_str, datetime):
                     entry_dt = entry_date_str
                else:
                     entry_dt = datetime.fromisoformat(str(entry_date_str))
                
                if entry_dt.tzinfo is None:
                    entry_dt = config.TIMEZONE.localize(entry_dt)
            except Exception as e:
                logger.error(f"Invalid date format in DB: {entry_date_str} - {e}")
                continue

            # Reconstitute Contract
            contract = Contract()
            contract.conId = contract_id
            contract.exchange = 'SMART'
            await self.ib.qualifyContractsAsync(contract)
            
            # Get Current Price of Option
            mid_price, _ = await execution.get_mid_price(self.ib, contract)
            if mid_price is None:
                continue
            
            # Calc PnL
            pnl_pct = (mid_price - entry_price) / entry_price
            
            # Determine TP Target based on Days Held using DB Tiers
            days_held = (now - entry_dt).days
            target_pnl = 9.99 # Logic should prevent exit if no tier matches
            
            # Find matching tier
            for tier in tiers:
                if tier['days_min'] <= days_held <= tier['days_max']:
                    target_pnl = tier['target_pnl']
                    break
            
            # CHECK 1: Take Profit (Dynamic)
            if pnl_pct >= target_pnl:
                logger.info(f"[Exit] Stepped TP Triggered for {contract.localSymbol}: {pnl_pct:.2%} >= {target_pnl:.2%} (Held {days_held}d)")
                trade = await execution.place_limit_order(self.ib, contract, 'SELL', int(pos['quantity']), mid_price)
                if trade and trade.orderStatus.status == 'Filled':
                     await self.db.close_trade(contract_id, now, trade.orderStatus.avgFillPrice, f'TP_Tier_{days_held}d')
                     await self.record_realized_profit(contract_id, entry_price, trade.orderStatus.avgFillPrice, int(pos['quantity']), trade.fills)
                     await self.check_and_invest_profits()
                return 

            # CHECK 2: Time Exit (Shield - Force Exit)
            time_exit_days = self.settings['time_exit_days']
            if days_held >= time_exit_days:
                logger.warning(f"[Exit] TIME LIMIT Triggered for {contract.localSymbol}: {days_held} days >= {time_exit_days}")
                trade = await execution.place_limit_order(self.ib, contract, 'SELL', int(pos['quantity']), mid_price)
                if trade and trade.orderStatus.status == 'Filled':
                     await self.db.close_trade(contract_id, now, trade.orderStatus.avgFillPrice, 'TimeLimit')
                     await self.record_realized_profit(contract_id, entry_price, trade.orderStatus.avgFillPrice, int(pos['quantity']), trade.fills)
                     await self.check_and_invest_profits()
                return

    async def process_entry_signal(self, force=False, quantity=1):
        """
        Validates Entry:
        1. One Trade Per Day check (unless forced, e.g. during ROLL).
        2. Max Positions check.
        3. Find LEAPS.
        4. Execute.
        """
        # Global Checks
        if not force and await self.db.has_traded_today(config.SYMBOL, config.TIMEZONE):
            logger.info("[Entry] Skipped: Already traded today (One Trade Per Day Rule).")
            return

        open_positions = await self.db.get_open_positions()
        max_positions = self.settings['max_positions']
        # If it's a ROLL rebalance, we check max_positions PLUS ONE because we just sold one
        # but the DB might not have updated if there's a delay, OR we just want to ensure
        # the slot we just vacated is available. Since we sold first, len(open_positions) 
        # should already be below max_positions.
        if len(open_positions) >= max_positions:
            logger.info(f"[Entry] Skipped: Max positions reached ({len(open_positions)}/{max_positions}).")
            return

        # Find Contract
        contract = await self.find_leaps()
        if not contract:
            logger.warning("[Entry] No suitable LEAPS contract found.")
            return

        # Execute
        logger.info(f"[Entry] Attempting to BUY {contract.localSymbol} (Qty: {quantity})...")
        # Get price for limit
        mid, _ = await execution.get_mid_price(self.ib, contract)
        if mid:
            trade = await execution.place_limit_order(self.ib, contract, 'BUY', quantity, mid)
            if trade:
                 if trade.orderStatus.status == 'Filled':
                     now = datetime.now(config.TIMEZONE)
                     await self.db.save_trade({
                        'contract_id': contract.conId, 
                        'symbol': config.SYMBOL, 
                        'entry_date': now, 
                        'entry_price': trade.orderStatus.avgFillPrice, 
                        'quantity': quantity
                     })
                     logger.info(f"[Entry] SUCCESS. Saved to DB.")
                 else:
                     logger.warning("[Entry] Order not filled.")


    async def find_leaps(self):
        """
        Industrial Scanning for LEAPS:
        1. All-strike Scan (No hardcoded anchors).
        2. Greeks Retry Mechanism.
        3. Mandatory Spread/Liquidity Filter (3%).
        4. Delta Precision Filter (0.05).
        """
        self.ib.reqMarketDataType(4) # Frozen/Delayed is fine for scanning
        
        target_delta = self.settings['target_delta']
        tolerance = self.settings['delta_tolerance']
        max_spread = self.settings['max_option_spread']
        min_expiry = self.settings['min_expiry_days']

        # Get Option Chain
        chains = await self.ib.reqSecDefOptParamsAsync(self.qqq_contract.symbol, '', self.qqq_contract.secType, self.qqq_contract.conId)
        smart_chains = [c for c in chains if c.exchange == 'SMART']
        if not smart_chains: 
            await self.db.add_alert('ERROR', "No SMART option chain found for QQQ")
            return None
        chain = smart_chains[0]

        # Filter Expirations
        now = datetime.now()
        valid_exp = [e for e in chain.expirations if (datetime.strptime(e, '%Y%m%d') - now).days >= min_expiry]
        if not valid_exp:
            await self.db.add_alert('WARN', f"No expirations found > {min_expiry} days.")
            return None
        
        target_exp = sorted(valid_exp)[0]
        
        # Get All Strikes around ATM (100% price)
        ticker = self.ib.reqMktData(self.qqq_contract, '', True, False)
        await asyncio.sleep(1)
        ref_price = ticker.last if ticker.last > 0 else (ticker.close if ticker.close > 0 else self.prev_close)
        
        # Select 30 strikes around ATM to find Delta anywhere from 0.2 to 0.8
        all_strikes = sorted(chain.strikes)
        closest_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - ref_price))
        search_strikes = all_strikes[max(0, closest_idx-15) : min(len(all_strikes), closest_idx+15)]
        
        candidates = []
        for strike in search_strikes:
            c = Contract(symbol=config.SYMBOL, secType='OPT', lastTradeDateOrContractMonth=target_exp, strike=strike, right='C', exchange='SMART')
            candidates.append(c)
        
        qualified = await self.ib.qualifyContractsAsync(*candidates)
        
        # Request Data with Retries for Greeks
        tickers = []
        for c in qualified:
            # 13: model greeks, 101: open interest
            tickers.append(self.ib.reqMktData(c, '13,101', True, False))
        
        best_contract = None
        min_delta_diff = 999
        max_oi = -1

        # Retry Loop for Greeks (3 times, 1s interval)
        for attempt in range(3):
            await asyncio.sleep(1.5)
            logger.info(f"[Scanner] Greek Data Attempt {attempt+1}/3...")
            
            for t in tickers:
                # 1. Delta Check
                delta = None
                if t.modelGreeks and t.modelGreeks.delta:
                    delta = t.modelGreeks.delta
                
                if delta is None: continue
                
                diff = abs(delta - target_delta)
                if diff > tolerance: continue
                
                # 2. Spread Check (Liquidity Guard)
                mid = (t.bid + t.ask) / 2 if (t.bid > 0 and t.ask > 0) else 0
                if mid <= 0: continue
                
                spread_pct = (t.ask - t.bid) / mid
                if spread_pct > max_spread:
                    # Log once for awareness
                    if attempt == 0:
                        logger.debug(f"Skipping {t.contract.localSymbol}: Spread {spread_pct:.2%} > {max_spread:.2%}")
                    continue
                
                # 3. Selection (Tie-break by Open Interest)
                oi = t.openInterest or 0
                if diff < min_delta_diff:
                    min_delta_diff = diff
                    best_contract = t.contract
                    max_oi = oi
                elif abs(diff - min_delta_diff) < 0.001: # Same delta diff, pick higher liquidity
                    if oi > max_oi:
                        best_contract = t.contract
                        max_oi = oi
            
            if best_contract: break # Found one

        # Final Decision
        if not best_contract:
            msg = f"Target Delta {target_delta} (±{tolerance}) unavailable. Safety/Liquidity Guard triggered."
            logger.warning(f"[Scanner] {msg}")
            await self.db.add_alert('WARN', msg)
            return None
            
        logger.info(f"[Scanner] SELECTED: {best_contract.localSymbol} (Diff: {min_delta_diff:.4f}, OI: {max_oi})")
        return best_contract

    async def record_realized_profit(self, contract_id, entry_price, exit_price, quantity, fills):
        """
        Calculates net profit (considering commissions) and updates DB.
        """
        commission = 0.0
        for f in fills:
            if f.commissionReport:
                commission += f.commissionReport.commission
        
        # Profit = (Exit - Entry) * Qty * 100 - Commission
        net_profit = (exit_price - entry_price) * quantity * 100 - commission
        
        current_total = await self.db.get_setting('leaps_realized_profit', 0.0)
        new_total = float(current_total) + net_profit
        await self.db.set_setting('leaps_realized_profit', str(new_total))
        self.settings['leaps_realized_profit'] = new_total
        
        logger.info(f"[Profit] Recorded Net Profit: ${net_profit:.2f}. New Total: ${new_total:.2f} (Commission: ${commission:.2f})")

    async def check_and_invest_profits(self):
        """
        Automatically invests accumulated profits into QQQM stock.
        """
        enabled = await self.db.get_setting('auto_invest_qqqm', 0)
        if not int(enabled):
            return

        total_profit = await self.db.get_setting('leaps_realized_profit', 0.0)
        invested = await self.db.get_setting('qqqm_invested_capital', 0.0)
        min_threshold = await self.db.get_setting('auto_invest_min_threshold', 500.0)
        
        available = float(total_profit) - float(invested)
        
        if available < float(min_threshold):
            logger.info(f"[AutoInvest] Available profit ${available:.2f} is below threshold ${min_threshold:.2f}")
            return

        # 1. Qualify QQQM
        qqqm = Stock('QQQM', 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(qqqm)
        
        # 2. Get Price
        price, _ = await execution.get_mid_price(self.ib, qqqm)
        if not price or price <= 0:
            logger.warning("[AutoInvest] Could not get price for QQQM")
            return

        # 3. Calculate Quantity
        shares = int(available // price)
        if shares <= 0:
            logger.info(f"[AutoInvest] Not enough profit (${available:.2f}) for 1 share of QQQM (${price:.2f})")
            return

        # 4. Execute Buy
        logger.info(f"[AutoInvest] Investing ${available:.2f} into {shares} shares of QQQM @ ${price:.2f}")
        trade = await execution.place_limit_order(self.ib, qqqm, 'BUY', shares, price)
        
        if trade and trade.orderStatus.status == 'Filled':
            actual_cost = trade.orderStatus.avgFillPrice * shares
            # We don't subtract commissions from 'invested' bucket, we just track capital deployed
            new_invested = float(invested) + actual_cost
            await self.db.set_setting('qqqm_invested_capital', str(new_invested))
            self.settings['qqqm_invested_capital'] = new_invested
            logger.info(f"[AutoInvest] SUCCESS: {shares} QQQM bought. Total Invested: ${new_invested:.2f}")
        else:
            logger.warning("[AutoInvest] Buy order failed or cancelled.")
