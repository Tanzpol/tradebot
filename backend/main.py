"""
Главное приложение trading bot v2
- FastAPI сервер для UI
- WebSocket для real-time данных (MAINNET)
- Управление процессами сделок (TESTNET)
- Market analysis и entry signals
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Импорты наших модулей
from shared_state import shared_state
from websocket_client import BinanceWebSocketClient
from trade_process import TradeProcessManager
from trade_logic import trade_logic, TradePhase
from risk_calculator import risk_calculator
from binance_client import BinanceRESTClient

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("../logs/main.log")
    ]
)

logger = logging.getLogger(__name__)

# Глобальные переменные
ws_client: Optional[BinanceWebSocketClient] = None
process_manager: Optional[TradeProcessManager] = None
binance_client: Optional[BinanceRESTClient] = None
market_analyzer_task: Optional[asyncio.Task] = None
bot_running = False

# Настройки из .env
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
TARGET_PROFIT = float(os.getenv("TARGET_PROFIT_USD", "50.0"))
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "10"))

# Pydantic модели для API
class TradeCreateRequest(BaseModel):
    symbol: str = Field(default="BTCUSDC", description="Trading symbol")
    trade_amount_usd: float = Field(gt=0, description="Amount in USD to trade")
    target_profit_usd: float = Field(default=TARGET_PROFIT, description="Target profit in USD")

class SystemStatus(BaseModel):
    bot_running: bool
    websocket_connected: bool
    active_trades: int
    available_balance: float
    current_btc_price: Optional[float]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    
    # Startup
    logger.info("🚀 Starting Trading Bot v2...")
    logger.info("📡 WebSocket: MAINNET (prices), 💰 Trading: TESTNET (safe)")
    
    global ws_client, process_manager, binance_client
    
    try:
        # Проверяем настройки
        if not API_KEY or not API_SECRET:
            raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET must be set")
        
        # Инициализируем компоненты
        binance_client = BinanceRESTClient(API_KEY, API_SECRET, TESTNET)
        process_manager = TradeProcessManager(API_KEY, API_SECRET, TESTNET)
        ws_client = BinanceWebSocketClient(API_KEY, API_SECRET, TESTNET)
        
        # Проверяем подключение к API
        if not await binance_client.test_connection():
            raise ConnectionError("Cannot connect to Binance API")
        
        # Настраиваем callbacks для WebSocket
        ws_client.set_price_callback(handle_price_update)
        ws_client.set_balance_callback(handle_balance_update)
        ws_client.set_order_callback(handle_order_update)
        
        # 🔥 НОВОЕ: Запускаем WebSocket для цен ВСЕГДА при старте
        if ws_client:
            await ws_client.start("BTCUSDC")
        
        # Запускаем автосохранение состояния
        await shared_state.start_auto_save()
        
        logger.info("✅ Trading Bot v2 initialized successfully")
        logger.info("📊 Price monitoring active (independent of bot status)")
        
        yield
        
    except Exception as e:
        logger.error(f"Failed to initialize Trading Bot v2: {e}")
        raise
    
    # Shutdown
    logger.info("Shutting down Trading Bot v2...")
    
    try:
        # Останавливаем бота если работает
        await stop_bot()
        
        # Закрываем WebSocket
        if ws_client:
            await ws_client.stop()
        
        # Останавливаем все процессы сделок
        if process_manager:
            process_manager.cleanup_all_processes()
        
        # Останавливаем автосохранение
        await shared_state.stop_auto_save()
        
        # Сохраняем финальное состояние
        shared_state.save_state(force=True)
        
        # Закрываем HTTP клиент
        if binance_client:
            await binance_client.close()
        
        logger.info("Trading Bot v2 shutdown complete")
        
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# Создаем FastAPI приложение
app = FastAPI(
    title="Trading Bot v2",
    description="Multi-process cryptocurrency trading bot with WebSocket support",
    version="2.0.0",
    lifespan=lifespan
)

# Настраиваем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# WebSocket callback функции
async def handle_price_update(symbol: str, price: float, data: Dict):
    """Обработчик обновления цен"""
    logger.debug(f"Price update: {symbol} = {price}")
    
    # Здесь можно добавить логику для анализа сигналов
    # Пока просто обновляем shared_state (это уже делается в ws_client)


async def handle_balance_update(balances: Dict):
    """Обработчик обновления баланса"""
    logger.info(f"Balance updated: {len(balances)} assets")


async def handle_order_update(order_info: Dict):
    """Обработчик обновления ордеров"""
    logger.info(f"Order update: {order_info['symbol']} {order_info['side']} {order_info['status']}")


# Market analyzer task
async def market_analyzer_task_func():
    """Анализирует рынок и создает сигналы для входа"""
    
    while bot_running:
        try:
            # Получаем текущие рыночные данные
            market_data = shared_state._system_state.market_data.get("BTCUSDC")
            if not market_data:
                await asyncio.sleep(60)
                continue
            
            current_price = market_data["price"]
            
            # Получаем исторические данные для индикаторов
            klines = await binance_client.get_klines("BTCUSDC", "15m", 50)
            if not klines:
                logger.warning("No klines data for analysis")
                await asyncio.sleep(60)
                continue
            
            # Рассчитываем простые индикаторы (можно расширить)
            indicators = await calculate_simple_indicators(klines)
            
            # Получаем доступный баланс
            usdt_balance = await binance_client.get_balance("USDT")
            available_balance = usdt_balance["free"] if usdt_balance else 0
            
            # Проверяем количество активных сделок
            active_trades = len(shared_state.get_all_trades())
            
            if active_trades >= MAX_CONCURRENT_TRADES:
                logger.debug(f"Max trades limit reached: {active_trades}/{MAX_CONCURRENT_TRADES}")
                await asyncio.sleep(60)
                continue
            
            # Анализируем сигнал входа
            signal = await trade_logic.analyze_entry_signal(
                symbol="BTCUSDC",
                current_price=current_price,
                market_indicators=indicators,
                available_balance=available_balance
            )
            
            if signal and available_balance > 50:  # Минимум $50 для сделки
                logger.info(f"Entry signal detected: {signal.reasons}")
                
                # Рассчитываем размер позиции
                position_calc = risk_calculator.calculate_position_size(
                    available_balance=available_balance,
                    entry_price=signal.entry_price,
                    target_profit_usd=signal.target_profit_usd,
                    risk_percent_of_balance=2.0
                )
                
                if position_calc["success"]:
                    # Создаем сделку автоматически
                    await create_trade_internal(
                        symbol=signal.symbol,
                        trade_amount_usd=position_calc["trade_amount_usd"],
                        target_profit_usd=signal.target_profit_usd
                    )
            
        except Exception as e:
            logger.error(f"Error in market analyzer: {e}")
        
        await asyncio.sleep(60)  # Анализируем каждую минуту


async def calculate_simple_indicators(klines: List) -> Dict:
    """Рассчитывает простые индикаторы для анализа"""
    
    if not klines or len(klines) < 20:
        return {"rsi": 50, "macd": 0, "macdsignal": 0}
    
    # Извлекаем цены закрытия
    closes = [float(kline[4]) for kline in klines]
    
    # Простой RSI расчет
    def calculate_rsi(prices, period=14):
        if len(prices) < period + 1:
            return 50
        
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    # Простой MACD расчет
    def calculate_ema(prices, period):
        if len(prices) < period:
            return prices[-1] if prices else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[-period:]) / period
        
        for price in prices[-period+1:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd = ema12 - ema26
    
    return {
        "rsi": calculate_rsi(closes),
        "macd": macd,
        "macdsignal": macd * 0.9,  # Упрощенная signal line
        "ema12": ema12,
        "ema26": ema26
    }


# API эндпоинты

@app.get("/api/health")
async def health_check():
    """Проверка здоровья системы"""
    try:
        system_info = shared_state.get_system_info()
        
        # Принудительно получаем текущую цену BTC
        current_btc_price = None
        try:
            if binance_client:
                ticker = await binance_client.get_symbol_price("BTCUSDC")
                if ticker:
                    current_btc_price = float(ticker["price"])
                    shared_state.update_market_data("BTCUSDC", current_btc_price)
        except:
            pass
        
        return JSONResponse({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "bot_running": bot_running,
            "websocket_connected": ws_client.is_connected() if ws_client else False,
            "active_trades": system_info["active_trades_count"],
            "testnet": TESTNET,
            "websocket_source": "MAINNET",
            "trading_source": "TESTNET" if TESTNET else "MAINNET"
        })
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)


@app.get("/api/status", response_model=SystemStatus)
async def get_system_status():
    """Получить статус системы"""
    try:
        system_info = shared_state.get_system_info()
        
        # Принудительно получаем текущую цену BTC
        current_btc_price = None
        try:
            if binance_client:
                ticker = await binance_client.get_symbol_price("BTCUSDC")
                if ticker:
                    current_btc_price = float(ticker["price"])
                    shared_state.update_market_data("BTCUSDC", current_btc_price)
        except:
            pass
        
        return SystemStatus(
            bot_running=bot_running,
            websocket_connected=ws_client.is_connected() if ws_client else False,
            active_trades=system_info["active_trades_count"],
            available_balance=system_info.get("available_balance", 0),
            current_btc_price=current_btc_price or system_info.get("current_btc_price")
        )
        
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/trades")
async def get_trades():
    """Получить информацию о всех сделках"""
    try:
        trades_summary = trade_logic.get_all_trades_summary()
        return JSONResponse(trades_summary)
        
    except Exception as e:
        logger.error(f"Error getting trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/balance")
async def get_balance():
    """Получить баланс аккаунта"""
    try:
        balances = await binance_client.get_account_balances()
        
        # Фильтруем только ненулевые балансы
        non_zero_balances = [
            {
                "asset": b["asset"],
                "free": float(b["free"]),
                "locked": float(b["locked"]),
                "total": float(b["free"]) + float(b["locked"])
            }
            for b in balances
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        
        return JSONResponse(non_zero_balances)
        
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trade")
async def create_trade(request: TradeCreateRequest):
    """Создать новую сделку"""
    try:
        result = await create_trade_internal(
            symbol=request.symbol,
            trade_amount_usd=request.trade_amount_usd,
            target_profit_usd=request.target_profit_usd
        )
        
        return JSONResponse(result)
        
    except Exception as e:
        logger.error(f"Error creating trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def create_trade_internal(symbol: str, trade_amount_usd: float, target_profit_usd: float) -> Dict:
    """Внутренняя функция создания сделки"""
    
    # Проверяем лимиты
    active_trades = len(shared_state.get_all_trades())
    if active_trades >= MAX_CONCURRENT_TRADES:
        return {"success": False, "error": f"Max trades limit reached: {active_trades}/{MAX_CONCURRENT_TRADES}"}
    
    # Получаем текущую цену
    price_data = await binance_client.get_symbol_price(symbol)
    if not price_data:
        return {"success": False, "error": "Cannot get current price"}
    
    current_price = float(price_data["price"])
    
    # Получаем балансы для проверки BNB
    usdt_balance = await binance_client.get_balance("USDT")
    bnb_balance = await binance_client.get_balance("BNB")
    
    if not usdt_balance or usdt_balance["free"] < trade_amount_usd:
        return {"success": False, "error": "Insufficient USDT balance"}
    
    bnb_amount = bnb_balance["free"] if bnb_balance else 0
    
    # Создаем торговый сигнал
    from trade_logic import TradeSignal
    import time
    
    signal = TradeSignal(
        symbol=symbol,
        entry_price=current_price,
        target_profit_usd=target_profit_usd,
        confidence=0.8,  # Для ручных сделок высокая уверенность
        reasons=["manual_trade"],
        timestamp=time.time()
    )
    
    # Получаем цену BNB
    bnb_price_data = await binance_client.get_symbol_price("BNBUSDT")
    bnb_price = float(bnb_price_data["price"]) if bnb_price_data else 300.0
    
    # Создаем состояние сделки
    success, trade_state, message = await trade_logic.create_trade_state(
        signal=signal,
        trade_amount_usd=trade_amount_usd,
        bnb_balance=bnb_amount,
        bnb_price=bnb_price
    )
    
    if not success:
        return {"success": False, "error": message}
    
    # Создаем ордер на покупку
    btc_quantity = trade_amount_usd / current_price
    order_result = await binance_client.create_market_order(symbol, "BUY", btc_quantity)
    
    if not order_result:
        return {"success": False, "error": "Failed to create buy order"}
    
    # Обновляем состояние сделки
    trade_state.status = TradePhase.WAITING_PROFIT.value
    shared_state.add_trade(trade_state)
    
    # Запускаем процесс для управления сделкой
    process_started = process_manager.start_trade_process(trade_state.trade_id)
    
    if not process_started:
        logger.error(f"Failed to start process for trade {trade_state.trade_id}")
        # Пытаемся закрыть позицию
        await binance_client.create_market_order(symbol, "SELL", btc_quantity)
        shared_state.remove_trade(trade_state.trade_id)
        return {"success": False, "error": "Failed to start trade process"}
    
    logger.info(f"New trade created: {trade_state.trade_id}")
    
    return {
        "success": True,
        "trade_id": trade_state.trade_id,
        "symbol": symbol,
        "entry_price": current_price,
        "quantity": btc_quantity,
        "target_profit_usd": target_profit_usd,
        "order_result": order_result
    }


@app.post("/api/bot/start")
async def start_bot(background_tasks: BackgroundTasks):
    """Запустить торгового бота"""
    global bot_running, market_analyzer_task
    
    try:
        if bot_running:
            return JSONResponse({"message": "Bot is already running"})
        
        bot_running = True
        shared_state.set_bot_status(True)
        
        # Запускаем анализатор рынка
        market_analyzer_task = asyncio.create_task(market_analyzer_task_func())
        
        logger.info("Trading bot started with BTCUSDC monitoring")
        return JSONResponse({"message": "Trading bot started successfully"})
        
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bot/stop")
async def stop_bot():
    """Остановить торгового бота"""
    global bot_running, market_analyzer_task
    
    try:
        if not bot_running:
            return JSONResponse({"message": "Bot is not running"})
        
        bot_running = False
        shared_state.set_bot_status(False)
        
        # Останавливаем анализатор рынка
        if market_analyzer_task:
            market_analyzer_task.cancel()
            market_analyzer_task = None
        
        logger.info("Trading bot stopped")
        return JSONResponse({"message": "Trading bot stopped successfully"})
        
    except Exception as e:
        logger.error(f"Error stopping bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/processes")
async def get_process_status():
    """Получить статус процессов сделок"""
    try:
        if process_manager:
            status = process_manager.get_process_status()
            return JSONResponse(status)
        else:
            return JSONResponse({"error": "Process manager not initialized"})
            
    except Exception as e:
        logger.error(f"Error getting process status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Статические файлы (для frontend)
if os.path.exists("../frontend/dist"):
    app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="frontend")


# Точка входа
if __name__ == "__main__":
    # Создаем необходимые папки
    os.makedirs("data", exist_ok=True)
    os.makedirs("data/state", exist_ok=True)
    os.makedirs("data/trades", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    
    # Запускаем сервер
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        reload=os.getenv("DEBUG", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower()

    )
