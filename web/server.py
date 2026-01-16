from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from web import auth
from datetime import datetime
import logging
import os

# Logger
logger = logging.getLogger("WebServer")

app = FastAPI(title="QQQ LEAPS Bot Dashboard")

# Templates & Static
# Absolute paths are safer in Docker/execution contexts
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Mount static files only if directory exists (we use Tailwind CDN anyway)
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Shared State (Injected from main.py)
bot_ib = None
bot_db = None
bot_strategy = None # Optional, if we need direct memory access

def set_dependencies(ib_instance, db_instance, strategy_instance):
    global bot_ib, bot_db, bot_strategy
    bot_ib = ib_instance
    bot_db = db_instance
    bot_strategy = strategy_instance

# --- Middleware / Dependencies ---

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    
    # Strip "Bearer " if present (though cookies usually just value)
    if token.startswith("Bearer "):
        token = token.split(" ")[1]
        
    payload = auth.decode_token(token)
    if not payload:
        return None
    return payload.get("sub")

async def require_auth(request: Request):
    user = await get_current_user(request)
    if not user:
        # Check if we are in Setup Mode
        count = await bot_db.get_admin_count()
        if count == 0:
            raise HTTPException(status_code=307, headers={"Location": "/setup"})
        
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user

async def check_setup_mode(request: Request, call_next):
    # Global Middleware logic for redirects
    # Exception: Static files, Login, Setup endpoints
    path = request.url.path
    if path.startswith("/static") or path in ["/favicon.ico"]:
        return await call_next(request)
    
    # We can't easily wait for DB here in middleware if it's not initialized
    # But main.py ensures DB is ready before server starts.
    count = await bot_db.get_admin_count()
    
    if count == 0:
        if path not in ["/setup"]:
             return RedirectResponse("/setup", status_code=303)
    else:
        # Admin exists
        if path == "/setup": # Block re-entry to setup
             return RedirectResponse("/", status_code=303)
        
        # If accessing protected routes without Auth
        if path not in ["/login"]:
             user = await get_current_user(request)
             if not user:
                  return RedirectResponse("/login", status_code=303)

    response = await call_next(request)
    return response

app.middleware("http")(check_setup_mode)


# --- Routes ---

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/setup")
async def setup_action(username: str = Form(...), password: str = Form(...)):
    if await bot_db.get_admin_count() > 0:
        return JSONResponse({"error": "Admin already exists"}, status_code=400)
    
    hashed = auth.get_password_hash(password)
    success = await bot_db.create_admin(username, hashed)
    
    if success:
        # Login user immediately
        access_token = auth.create_access_token(data={"sub": username})
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
        return response
    
    return HTMLResponse("Error creating admin", status_code=500)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_action(username: str = Form(...), password: str = Form(...)):
    stored_hash = await bot_db.get_admin_password(username)
    if not stored_hash or not auth.verify_password(password, stored_hash):
        return templates.TemplateResponse("login.html", {"request": None, "error": "Invalid Credentials"})
    
    access_token = auth.create_access_token(data={"sub": username})
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Fetch Data for Initial Render
    qqq_price = 0.0
    # Try to get live price from Strategy Cache or IB
    if bot_strategy and bot_strategy.qqq_contract:
         # This might be blocking or need helper
         pass 
         
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "ib_connected": bot_ib.isConnected() if bot_ib else False
    })

# --- API ---

@app.get("/api/status")
async def api_status():
    """Returns real-time status for AJAX updates"""
    connected = bot_ib.isConnected() if bot_ib else False
    
    positions = await bot_db.get_open_positions()
    
    # Calculate Live PnL for positions if connected
    enriched_positions = []
    total_unrealized_pnl_usd = 0.0
    
    if connected:
        # We can't casually reqMktData here as it is async and might take time
        # Better to rely on Strategy loop updating a cache, or allow slight delay
        pass 

    # Settings
    settings = {}
    if bot_strategy:
        settings = bot_strategy.settings

    # New: Portfolio Data
    portfolio = None
    if bot_strategy:
        portfolio = await bot_strategy.get_all_holdings()

    return {
        "connected": connected,
        "positions": positions,
        "portfolio": portfolio,
        "settings": settings,
        "stats": {
            "leaps_realized_profit": settings.get('leaps_realized_profit', 0.0),
            "qqqm_invested_capital": settings.get('qqqm_invested_capital', 0.0)
        },
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/settings")
async def update_settings(data: dict):
    # data e.g. {"target_delta": 0.5}
    for k, v in data.items():
        # Infer type
        t = 'str'
        if isinstance(v, int): t = 'int'
        if isinstance(v, float): t = 'float'
        await bot_db.set_setting(k, v, t)
        
        # Update Strategy Cache Immediately
        if bot_strategy:
             bot_strategy.settings[k] = v
             
    return {"status": "ok"}

@app.get("/api/exit_tiers")
async def get_exit_tiers():
    """Returns current exit tier configuration"""
    tiers = await bot_db.get_exit_tiers()
    return {"tiers": tiers}

@app.post("/api/exit_tiers")
async def update_exit_tiers(data: dict):
    """
    Updates exit tiers. 
    Expects data: {"tiers": [{"days_min": 0, "days_max": 120, "target_pnl": 0.5}, ...]}
    """
    tiers = data.get("tiers", [])
    if tiers:
        await bot_db.update_exit_tiers(tiers)
        # We don't have a specific strategy cache for tiers yet, 
        # but strategy.py re-reads them every cycle.
    return {"status": "ok"}
