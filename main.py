
import asyncio
import logging
import sys
from datetime import datetime
from ib_insync import *
import config
import persistence
from strategy import Strategy

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Main")

async def run_bot():
    ib = IB()
    strategy = Strategy(ib)
    
    # Connection Loop
    while True:
        try:
            if not ib.isConnected():
                logger.info(f"Connecting to IBKR (Port {config.IB_PORT})...")
                try:
                    await ib.connectAsync(
                        config.IB_HOST, 
                        config.IB_PORT, 
                        clientId=config.CLIENT_ID,
                        timeout=10
                    )
                    logger.info("Connected.")
                except Exception as e:
                    logger.error(f"Connection failed: {e}. Retrying in {config.RECONNECT_DELAY}s...")
                    await asyncio.sleep(config.RECONNECT_DELAY)
                    continue

            # Initialization Loop
            init_failures = 0
            while strategy.prev_close is None:
                success = await strategy.initialize()
                if success:
                    logger.info("Strategy Initialized Successfully.")
                    break
                else:
                    init_failures += 1
                    logger.warning(f"Strategy Initialization Failed ({init_failures}/3).")
                    
                    if init_failures >= 3:
                        logger.error("ALARM: Initialization failed 3 times. Pausing for 15 minutes to avoid API spam.")
                        # Disconnect during pause? Or just sleep. Sleep is safer to keep loop simple.
                        # IB connection might timeout if idle, but loop handles that.
                        await asyncio.sleep(15 * 60) 
                        init_failures = 0 # Reset after long pause
                    else:
                        await asyncio.sleep(5) # Short retry delay

            # Main Strategy Loop
            # We want to run every 5 minutes
            logger.info("Starting Main Strategy Cycle...")
            while ib.isConnected():
                try:
                    await strategy.run_cycle()
                    
                    # Wait for next 5-min mark
                    # Current minute
                    now = datetime.now()
                    # Calculate minutes to next 5-min interval
                    # e.g. 14:02 -> next is 14:05. Sleep 3 mins.
                    # e.g. 14:05:01 -> next is 14:10. Sleep 4m 59s.
                    next_run_min = (now.minute // config.SCAN_INTERVAL_MIN + 1) * config.SCAN_INTERVAL_MIN
                    # Fix: Use timedelta from datetime, not asyncio
                    from datetime import timedelta 
                    next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=next_run_min)
                    if next_run_min >= 60: # Handle hour rollover roughly
                        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                        
                    sleep_seconds = (next_run - now).total_seconds()
                    # Ensure positive sleep
                    if sleep_seconds < 1: sleep_seconds = 60 
                    
                    logger.info(f"Cycle Complete. Sleeping {sleep_seconds:.1f}s until {next_run.strftime('%H:%M:%S')}...")
                    await asyncio.sleep(sleep_seconds)
                    
                except Exception as cycle_error:
                    logger.error(f"Error in Run Cycle: {cycle_error}")
                    await asyncio.sleep(60) # Error backoff

        except Exception as e:
            logger.error(f"Global Error: {e}")
            await asyncio.sleep(config.RECONNECT_DELAY)

if __name__ == '__main__':
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
