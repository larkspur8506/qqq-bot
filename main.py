
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

import uvicorn
from persistence import Database
import web.server

async def start_web_server():
    """Starts the FastAPI Web Server"""
    config = uvicorn.Config("web.server:app", host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def run_bot_logic(ib, strategy):
    """The original infinite loop for trading logic"""
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
                    try:
                        ib.reqMarketDataType(3)  # Ensure Delayed Data is used (fixes Error 10089 on reconnect)
                    except Exception as e:
                        logger.error(f"Failed to set market data type: {e}")
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
                        await asyncio.sleep(15 * 60) 
                        init_failures = 0 
                    else:
                        await asyncio.sleep(5) 

            # Main Strategy Loop
            logger.info("Starting Main Strategy Cycle...")
            while ib.isConnected():
                try:
                    await strategy.run_cycle()
                    
                    # Wait for next 5-min mark
                    now = datetime.now()
                    next_run_min = (now.minute // config.SCAN_INTERVAL_MIN + 1) * config.SCAN_INTERVAL_MIN
                    from datetime import timedelta 
                    next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=next_run_min)
                    if next_run_min >= 60: 
                        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                        
                    sleep_seconds = (next_run - now).total_seconds()
                    if sleep_seconds < 1: sleep_seconds = 60 
                    
                    logger.info(f"Cycle Complete. Sleeping {sleep_seconds:.1f}s until {next_run.strftime('%H:%M:%S')}...")
                    await asyncio.sleep(sleep_seconds)
                    
                except Exception as cycle_error:
                    logger.error(f"Error in Run Cycle: {cycle_error}")
                    await asyncio.sleep(60) 

        except Exception as e:
            logger.error(f"Global Error: {e}")
            await asyncio.sleep(config.RECONNECT_DELAY)

async def main():
    # 1. Initialize Database
    db = Database()
    await db.initialize()
    
    # 2. Initialize Bot Components
    ib = IB()
    strategy = Strategy(ib, db)
    
    # 3. Inject into Web Server
    web.server.set_dependencies(ib, db, strategy)
    
    # 4. Run Concurrently
    await asyncio.gather(
        run_bot_logic(ib, strategy),
        start_web_server()
    )

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
