
import asyncio
from datetime import datetime, timedelta
import logging
from ib_insync import *
import config
import persistence
import execution

logger = logging.getLogger(__name__)

class Strategy:
    def __init__(self, ib: IB):
        self.ib = ib
        self.qqq_contract = Stock(config.SYMBOL, 'SMART', 'USD')
        self.prev_close = None
        self.last_day_check = None 

    async def initialize(self):
        """
        Run startup tasks:
        1. Set Delayed Market Data (Type 3) if no subscription.
        2. Qualify QQQ contract.
        3. Fetch authoritative PrevClose (Daily Bar).
        Returns: True if successful, False otherwise.
        """
        logger.info("[Strategy] Initializing...")
        
        # Switch to Delayed Market Data (3) as requested by user
        self.ib.reqMarketDataType(3) 
        
        try:
            await self.ib.qualifyContractsAsync(self.qqq_contract)
            
            # Fetch 1 day of historical data to get yesterday's close
            # This is robust to mid-day restarts
            bars = await self.ib.reqHistoricalDataAsync(
                self.qqq_contract, 
                endDateTime='', 
                durationStr='1 D', 
                barSizeSetting='1 day', 
                whatToShow='TRADES', 
                useRTH=True
            )
        
            if bars:
                # The returned bar is "today" if market is open/closed, or yesterday?
                # ib_insync '1 D' usually returns the last completed day if strictly historical, 
                # but if endDateTime is empty, it returns up to now.
                # We specifically want the CLOSE of the PREVIOUS session.
                # Let's request 2 days to be safe and look at the one before today.
                 bars_2d = await self.ib.reqHistoricalDataAsync(
                    self.qqq_contract, 
                    endDateTime='', 
                    durationStr='2 D', 
                    barSizeSetting='1 day', 
                    whatToShow='TRADES', 
                    useRTH=True
                )
                 if len(bars_2d) >= 1:
                     # The last bar might be "today" (partial) or "yesterday" (if before open)
                     # We check the date.
                     today_str = datetime.now(config.TIMEZONE).date().isoformat()
                     
                     # Iterate backwards to find the first bar that is NOT today
                     found_prev = False
                     for bar in reversed(bars_2d):
                         bar_date_str = bar.date.isoformat()
                         if bar_date_str < today_str:
                             self.prev_close = bar.close
                             logger.info(f"[Strategy] PrevClose established from {bar.date}: {self.prev_close}")
                             found_prev = True
                             break
                     
                     if not found_prev:
                         # This implies both bars are today? Unlikely. 
                         # Fallback: Just take the first bar's close if it's the only one available and check date
                         logger.warning("[Strategy] Could not definitively identify previous day bar. Using first available close.")
                         self.prev_close = bars_2d[0].close
        
        except Exception as e:
            logger.error(f"[Strategy] Error during initialization: {e}")
            return False

        if self.prev_close is None:
            logger.error("[Strategy] CRITICAL: Could not fetch PrevClose. Bot cannot trade safely.")
            return False
        
        return True

    async def run_cycle(self):
        """
        The main logic executed every 5 minutes.
        """
        if self.prev_close is None:
            await self.initialize() # Retry init if failed previously
            if self.prev_close is None: 
                return

        # 1. Update Market Price
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
        if pct_change <= config.ENTRY_DROP_PCT:
            logger.info(f"[Signal] DROP DETECTED: {pct_change:.2%} <= {config.ENTRY_DROP_PCT:.2%}")
            await self.process_entry_signal()
        else:
            logger.info("[Signal] No entry signal.")

    async def manage_positions(self, current_underlying_price):
        """
        Checks TP and Time Exit for all open positions.
        """
        open_positions = persistence.load_open_positions()
        if not open_positions:
            return

        logger.info(f"[Manager] Checking {len(open_positions)} open positions...")
        
        now = datetime.now(config.TIMEZONE)

        for pos in open_positions:
            contract_id = int(pos['ContractID'])
            entry_price = float(pos['EntryPrice'])
            entry_date_str = pos['EntryDate'] 
            # Parse entry date - handle T separator if present
            try:
                entry_dt = datetime.fromisoformat(entry_date_str)
                # Ensure timezone aware
                if entry_dt.tzinfo is None:
                    entry_dt = config.TIMEZONE.localize(entry_dt)
            except ValueError:
                logger.error(f"Invalid date format in CSV: {entry_date_str}")
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
            
            # Determine TP Target based on Days Held
            days_held = (now - entry_dt).days
            target_pnl = 0.50 # Default high
            
            # 0-4 months (0-120 days): 50%
            # 4-6 months (121-180 days): 30%
            # 7-9 months (181-270 days): 10%
            if days_held <= 120:
                target_pnl = 0.50
            elif days_held <= 180:
                target_pnl = 0.30
            elif days_held < config.TIME_EXIT_DAYS:
                target_pnl = 0.10
            
            # CHECK 1: Take Profit (Dynamic)
            if pnl_pct >= target_pnl:
                logger.info(f"[Exit] Stepped TP Triggered for {contract.localSymbol}: {pnl_pct:.2%} >= {target_pnl:.2%} (Held {days_held}d)")
                trade = await execution.place_limit_order(self.ib, contract, 'SELL', int(pos['Quantity']), mid_price)
                if trade and trade.orderStatus.status == 'Filled':
                     persistence.update_trade_exit(contract_id, f'TP_Tier_{days_held}d', now, trade.orderStatus.avgFillPrice, (trade.orderStatus.avgFillPrice - entry_price)*100)
                return 

            # CHECK 2: Time Exit (Shield - Force Exit)
            if days_held >= config.TIME_EXIT_DAYS:
                logger.warning(f"[Exit] TIME LIMIT Triggered for {contract.localSymbol}: {days_held} days >= {config.TIME_EXIT_DAYS}")
                trade = await execution.place_limit_order(self.ib, contract, 'SELL', int(pos['Quantity']), mid_price)
                if trade and trade.orderStatus.status == 'Filled':
                     persistence.update_trade_exit(contract_id, 'TimeLimit', now, trade.orderStatus.avgFillPrice, (trade.orderStatus.avgFillPrice - entry_price)*100)
                return

    async def process_entry_signal(self):
        """
        Validates Entry:
        1. One Trade Per Day check.
        2. Max Positions check.
        3. Find LEAPS.
        4. Execute.
        """
        # Global Checks
        if persistence.has_traded_today(config.SYMBOL, config.TIMEZONE):
            logger.info("[Entry] Skipped: Already traded today (One Trade Per Day Rule).")
            return

        open_positions = persistence.load_open_positions()
        if len(open_positions) >= config.MAX_POSITIONS:
            logger.info(f"[Entry] Skipped: Max positions reached ({len(open_positions)}/{config.MAX_POSITIONS}).")
            return

        # Find Contract
        contract = await self.find_leaps()
        if not contract:
            logger.warning("[Entry] No suitable LEAPS contract found.")
            return

        # Execute
        logger.info(f"[Entry] Attempting to BUY {contract.localSymbol}...")
        # Get price for limit
        mid, _ = await execution.get_mid_price(self.ib, contract)
        if mid:
            trade = await execution.place_limit_order(self.ib, contract, 'BUY', 1, mid)
            if trade:
                 # We wait a bit or assume the execution module handles the wait. 
                 # execution.place_limit_order waits for fill or cancel.
                 if trade.orderStatus.status == 'Filled':
                     now = datetime.now(config.TIMEZONE)
                     persistence.save_trade(contract.conId, config.SYMBOL, now, trade.orderStatus.avgFillPrice, 1)
                     logger.info(f"[Entry] SUCCESS. Saved to DB.")
                 else:
                     logger.warning("[Entry] Order not filled.")


    async def find_leaps(self):
        """
        Scans Option Chain for:
        - Expiry > 365 days
        - Delta ~ 0.6
        """
        self.ib.reqMarketDataType(4) # Frozen/Delayed is fine for scanning, but we prefer 1 (Live) for execution
        
        # Get Chains
        chains = await self.ib.reqSecDefOptParamsAsync(self.qqq_contract.symbol, '', self.qqq_contract.secType, self.qqq_contract.conId)
        
        # Filter for SMART exchange
        smart_chains = [c for c in chains if c.exchange == 'SMART']
        if not smart_chains: return None
        chain = smart_chains[0]

        # Filter Expirations > 365 days
        now = datetime.now()
        valid_expirations = []
        for exp in chain.expirations:
            # exp format YYYYMMDD
            d = datetime.strptime(exp, '%Y%m%d')
            if (d - now).days >= config.MIN_EXPIRY_DAYS:
                valid_expirations.append(exp)
        
        if not valid_expirations:
            logger.warning("[Scanner] No expirations > 365 days found.")
            return None
            
        # Select the nearest valid expiration (shortest LEAP)
        # Or farthest? Spec says "> 365". Usually we pick the one closest to 1 year or just the first one.
        # Let's pick the first valid one to ensure liquidity
        target_exp = sorted(valid_expirations)[0]
        logger.info(f"[Scanner] Selected Expiry: {target_exp}")
        
        # Get Strikes and Deltas
        # To get Delta, we need Option computation or just estimate ITM
        # ib_insync has calculateImpliedVolatility or we can just fetch the chain
        
        # For simplicity and speed:
        # A 0.6 Delta Call is ITM. QQQ Price * (1 - OTM%)?
        # Roughly, 0.5 Delta is ATM. 0.6 Delta is slightly ITM.
        # Let's request the Option Chain for this expiry.
        
        strikes = chain.strikes
        # We need the underlying price to guess the strike range
        ticker = self.ib.reqMktData(self.qqq_contract, '', True, False)
        # Wait for tick
        await asyncio.sleep(1)
        ref_price = ticker.last if ticker.last else self.prev_close
        
        if not ref_price: return None

        # Look for strikes slightly below current price (ITM)
        # 0.6 Delta implies we are ITM. 
        # Heuristic: Strike ~ 95% of Current Price usually gives ~0.6 Delta for LEAPS (very rough)
        # Better: Request Greeks.
        
        # Let's construct a few contracts around 90-95% moneyness and ask for Greeks
        target_strike = ref_price * 0.95
        # Find closest strikes
        closest_strikes = sorted(strikes, key=lambda x: abs(x - target_strike))[:5]
        
        candidates = []
        for strike in closest_strikes:
            contract = Contract(symbol=config.SYMBOL, secType='OPT', lastTradeDateOrContractMonth=target_exp, strike=strike, right='C', exchange='SMART')
            await self.ib.qualifyContractsAsync(contract)
            candidates.append(contract)
            
        # Request Data and Greeks for candidates
        best_contract = None
        min_delta_diff = 999
        
        for contract in candidates:
             # Requesting tick data with GenericTickList '100' (Option Volume), '101' (Open Interest), '106' (Implied Vol)
             # But 'delta' comes with regular market data if possible or reqGreeks
             # ib_insync Ticker has 'modelGreeks' if enabled
             t = self.ib.reqMktData(contract, '13', True, False) # 13 = model greeks
             
             # Wait a moment
        
        # Waiting for multiple tickers is tricky in a loop.
        await asyncio.sleep(2)
        
        for contract in candidates:
            # We need to find the ticker associated
            t = self.ib.ticker(contract)
            if t and t.modelGreeks and t.modelGreeks.delta:
                delta = t.modelGreeks.delta
                diff = abs(delta - config.TARGET_DELTA)
                # Ensure it's positive delta for Call (it should be)
                if diff < min_delta_diff and diff <= config.DELTA_TOLERANCE:
                     min_delta_diff = diff
                     best_contract = contract
                     logger.info(f"[Scanner] Found Candidate: {contract.localSymbol} (Delta={delta:.3f})")

        if not best_contract and candidates:
             # Fallback if Greeks fail: Pick the one closest to our heuristic strike
             logger.warning("[Scanner] Greeks data unavailable. Using heuristic strike selection.")
             best_contract = candidates[0]

        return best_contract

