import asyncio
from ib_insync import *
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestLeaps")

async def test_leaps_scanner():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4004, clientId=99)
        logger.info("Connected to IBKR")

        qqq = Stock('QQQ', 'SMART', 'USD')
        await ib.qualifyContractsAsync(qqq)

        logger.info("Fetching option chains...")
        chains = await ib.reqSecDefOptParamsAsync(qqq.symbol, '', qqq.secType, qqq.conId)
        chain = next(c for c in chains if c.exchange == 'SMART')

        now = datetime.now()
        leaps_expiries = [e for e in chain.expirations if (datetime.strptime(e, '%Y%m%d') - now).days >= 365]

        if not leaps_expiries:
            logger.error("No LEAPS expiries found (> 365 days)!")
            return

        target_exp = sorted(leaps_expiries)[0]
        logger.info(f"Targeting expiry: {target_exp}")

        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(qqq, '', True, False)
        await asyncio.sleep(2)
        ref_price = ticker.marketPrice()
        logger.info(f"QQQ Ref Price: {ref_price}")

        strikes = sorted(chain.strikes)
        closest_strike = min(strikes, key=lambda s: abs(s - ref_price))
        idx = strikes.index(closest_strike)
        test_strikes = strikes[max(0, idx-5) : idx+5]

        contracts = [
            Contract(symbol='QQQ', secType='OPT', lastTradeDateOrContractMonth=target_exp,
                     strike=s, right='C', exchange='SMART')
            for s in test_strikes
        ]
        qualified = await ib.qualifyContractsAsync(*contracts)

        logger.info(f"Qualifed {len(qualified)} contracts. Requesting Greeks...")

        ib.reqMarketDataType(4)
        tickers = [ib.reqMktData(c, '13,101', False, False) for c in qualified]

        for i in range(1, 6):
            await asyncio.sleep(2)
            logger.info(f"Waiting for data (Attempt {i}/5)...")
            for t in tickers:
                mid = (t.bid + t.ask) / 2
                spread = (t.ask - t.bid) / mid if mid > 0 else 0
                delta = t.modelGreeks.delta if t.modelGreeks else "N/A"

                logger.info(
                    f"Contract: {t.contract.localSymbol} | "
                    f"Bid: {t.bid} | Ask: {t.ask} | "
                    f"Mid: {mid:.2f} | Spread: {spread:.2%} | "
                    f"Delta: {delta}"
                )

    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(test_leaps_scanner())
