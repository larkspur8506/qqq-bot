
import asyncio
from ib_insync import *
import config
import logging

# Setup logging
logger = logging.getLogger(__name__)

async def get_mid_price(ib: IB, contract: Contract):
    """
    Fetches real-time market data to calculate (Bid + Ask) / 2.
    Returns:
        mid_price (float) or None if data is invalid/unavailable.
        market_data (Ticker) object for further inspection if needed.
    """
    # Request market data (snapshot=True for one-time fetch)
    ticker = ib.reqMktData(contract, '', True, False)
    
    # Wait for data to populate (IBKR is async)
    attempts = 0
    while attempts < 20: # Wait up to ~2 seconds
        if ticker.bid > 0 and ticker.ask > 0:
            break
        await asyncio.sleep(0.1)
        attempts += 1
        
    bid = ticker.bid
    ask = ticker.ask

    # Validation: Ensure positive bid/ask and spread sanity
    if bid <= 0 or ask <= 0:
        logger.warning(f"[Execution] Invalid market data for {contract.localSymbol}: Bid={bid}, Ask={ask}")
        return None, ticker
    
    if bid > ask:
        logger.warning(f"[Execution] Crossed market for {contract.localSymbol}: Bid={bid} > Ask={ask}")
        return None, ticker

    mid_price = (bid + ask) / 2.0
    return mid_price, ticker

async def place_limit_order(ib: IB, contract: Contract, action: str, quantity: int, limit_price: float, max_spread: float | None = None):
    """
    Places a Limit Order with safety checks and timeout.

    Args:
        ib: Active IB connection.
        contract: The contract to trade.
        action: 'BUY' or 'SELL'.
        quantity: Number of contracts.
        limit_price: limit price.
        max_spread: Optional max spread ratio (e.g., 0.03 for 3%).
                    If None, uses default 0.05 for stocks or configurable value for options.

    Returns:
        trade (Trade): The trade object if successful (filled or submitted), None if aborted.
    """
    # 1. Re-validate Market Data (Spread Check)
    mid, ticker = await get_mid_price(ib, contract)

    if mid is None:
        logger.error(f"[Execution] Aborted {action}: Could not get valid Price.")
        return None

    bid = ticker.bid
    ask = ticker.ask
    spread = ask - bid
    spread_ratio = spread / mid

    # SAFETY CHECK: Wide Spread
    # Use provided max_spread, or default 5% if not specified (e.g., for stocks)
    effective_max_spread = max_spread if max_spread is not None else 0.05
    if spread_ratio > effective_max_spread:
        logger.warning(f"[Execution] SPREAD PROTECTION: Spread {spread:.2f} ({spread_ratio:.2%}) > Max {effective_max_spread:.2%}. Aborting.")
        return None

    # SAFETY CHECK: Size Protection (Premium Cap)
    multiplier = 100 if contract.secType == 'OPT' else 1
    total_premium = limit_price * multiplier * quantity
    if action == 'BUY' and total_premium > config.MAX_PREMIUM:
        logger.warning(f"[Execution] SIZE PROTECTION: Premium ${total_premium:.2f} > Limit ${config.MAX_PREMIUM}. Aborting.")
        return None

    # 2. Place Order
    order = LimitOrder(action, quantity, limit_price)
    
    # Optional: Set OutsideRth=False to only trade regular hours? Default is False (RTH only).
    # order.outsideRth = False 

    trade = ib.placeOrder(contract, order)
    logger.info(f"[Execution] Placed {action} {quantity} {contract.localSymbol} @ {limit_price:.2f}")

    # 3. Wait for fill or timeout
    try:
        # Wait until order is filled or cancelled, with timeout
        # Using a loop to check status periodically to allow logging
        start_time = asyncio.get_event_loop().time()
        while not trade.isDone():
            await asyncio.sleep(1)
            if asyncio.get_event_loop().time() - start_time > config.ORDER_TIMEOUT:
                logger.warning(f"[Execution] Order TIMEOUT ({config.ORDER_TIMEOUT}s). Cancelling...")
                ib.cancelOrder(order)
                # Wait for cancellation confirmation
                await asyncio.sleep(2) 
                return trade # Return the trade in its current state (likely Cancelled)
                
        if trade.orderStatus.status == 'Filled':
             logger.info(f"[Execution] Order FILLED: {trade.orderStatus.filled} @ {trade.orderStatus.avgFillPrice}")
             return trade
        else:
             logger.warning(f"[Execution] Order finished with status: {trade.orderStatus.status}")
             return trade

    except Exception as e:
        logger.error(f"[Execution] Error monitoring order: {e}")
        return None
