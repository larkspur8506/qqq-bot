import asyncio
import math
from ib_insync import *
import config
import logging

logger = logging.getLogger(__name__)

async def get_mid_price(ib: IB, contract: Contract):
    """
    Enhanced price discovery:
    1. Real-time Bid/Ask mid price.
    2. Last/Close price fallback.
    3. Historical K-line (1h) as final fallback.
    """
    ib.reqMarketDataType(4)
    
    ticker = ib.reqMktData(contract, '', False, False)
    
    attempts = 0
    while attempts < 15:
        if ticker.bid > 0 and ticker.ask > 0:
            break
        await asyncio.sleep(0.1)
        attempts += 1
        
    mid = (ticker.bid + ticker.ask) / 2 if (ticker.bid > 0 and ticker.ask > 0) else ticker.last
    
    if mid is None or math.isnan(mid) or mid <= 0:
        mid = ticker.close if (ticker.close and ticker.close > 0) else mid
        
    if mid is None or math.isnan(mid) or mid <= 0:
        logger.info(f"[Execution] {contract.localSymbol} realtime data missing, fetching historical snapshot...")
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract, endDateTime='', durationStr='1 D',
                barSizeSetting='1 hour', whatToShow='MIDPOINT', useRTH=True
            )
            if bars:
                mid = bars[-1].close
                logger.info(f"[Execution] Historical fallback price: {mid}")
        except Exception as e:
            logger.error(f"[Execution] Historical data fetch failed: {e}")

    if mid is None or math.isnan(mid) or mid <= 0:
        logger.warning(f"[Execution] Cannot get valid price for {contract.localSymbol}")
        return None, ticker
    
    return mid, ticker

async def place_limit_order(ib: IB, contract: Contract, action: str, quantity: int, limit_price: float, max_spread: float | None = None):
    """
    Place limit order with price validation and spread protection.
    """
    if math.isnan(limit_price):
        logger.warning(f"[Execution] limit_price is nan, recalculating...")
        limit_price, ticker = await get_mid_price(ib, contract)
        if limit_price is None:
            return None
    else:
        _, ticker = await get_mid_price(ib, contract)

    if ticker.bid > 0 and ticker.ask > 0:
        mid = (ticker.bid + ticker.ask) / 2
        spread = ticker.ask - ticker.bid
        spread_ratio = spread / mid
        effective_max_spread = max_spread if max_spread is not None else 0.05
        
        if spread_ratio > effective_max_spread:
            logger.warning(f"[Execution] Spread too wide: {spread_ratio:.2%} > {effective_max_spread:.2%}. Aborting.")
            return None
    else:
        logger.info(f"[Execution] {contract.localSymbol} missing realtime spread data, using historical price.")

    multiplier = float(contract.multiplier) if contract.multiplier else 1
    total_premium = limit_price * multiplier * quantity
    if action == 'BUY' and total_premium > config.MAX_PREMIUM:
        logger.warning(f"[Execution] Size exceeded: ${total_premium:.2f} > ${config.MAX_PREMIUM}. Aborting.")
        return None

    order = LimitOrder(action, quantity, round(limit_price, 2))
    trade = ib.placeOrder(contract, order)
    logger.info(f"[Execution] Order sent: {action} {quantity} {contract.localSymbol} @ {limit_price:.2f}")

    try:
        start_time = asyncio.get_event_loop().time()
        while not trade.isDone():
            await asyncio.sleep(1)
            if asyncio.get_event_loop().time() - start_time > config.ORDER_TIMEOUT:
                logger.warning(f"[Execution] Order timeout ({config.ORDER_TIMEOUT}s), cancelling...")
                ib.cancelOrder(order)
                await asyncio.sleep(1)
                return trade
                
        if trade.orderStatus.status == 'Filled':
            logger.info(f"[Execution] Order filled: {trade.orderStatus.filled} @ {trade.orderStatus.avgFillPrice}")
            return trade
        else:
            logger.warning(f"[Execution] Order status: {trade.orderStatus.status}")
            return trade

    except Exception as e:
        logger.error(f"[Execution] Order monitoring error: {e}")
        return None
