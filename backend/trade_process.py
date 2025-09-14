"""
Отдельный процесс для управления одной сделкой
- Высокочастотный мониторинг (каждые 2-3 секунды)
- Автономная торговая логика
- Связь с main процессом через shared_state
- Создание и выполнение ордеров
"""

import asyncio
import logging
import multiprocessing
import os
import signal
import sys
import time
from typing import Optional, Dict, Any

# Добавляем путь для импорта
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared_state import shared_state, TradeState
from trade_logic import trade_logic, TradePhase
from risk_calculator import risk_calculator
from binance_client import BinanceRESTClient

logger = logging.getLogger(__name__)


class TradeProcess:
    """Процесс управления одной сделкой"""
    
    def __init__(self, trade_id: str, api_key: str, api_secret: str, testnet: bool = True):
        self.trade_id = trade_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        # Состояние процесса
        self.should_run = True
        self.process_id = os.getpid()
        self.last_price_update = 0
        self.error_count = 0
        self.max_errors = 10
        
        # Binance клиент
        self.binance_client = None
        
        # Настройки мониторинга
        self.price_check_interval = 2  # секунды
        self.state_save_interval = 10  # секунды
        self.last_state_save = time.time()
        
        # Устанавливаем обработчик сигналов для graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Обработчик сигналов для graceful shutdown"""
        logger.info(f"Process {self.process_id} received signal {signum}, shutting down...")
        self.should_run = False

    async def initialize(self) -> bool:
        """Инициализация процесса"""
        try:
            # Инициализируем Binance клиент
            self.binance_client = BinanceRESTClient(self.api_key, self.api_secret, self.testnet)
            
            # Проверяем подключение
            if not await self.binance_client.test_connection():
                logger.error("Failed to connect to Binance API")
                return False
            
            # Обновляем информацию о процессе в состоянии
            shared_state.update_trade(self.trade_id, process_id=self.process_id)
            
            logger.info(f"Trade process {self.process_id} initialized for trade {self.trade_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing trade process: {e}")
            return False

    async def run(self):
        """Основной цикл процесса"""
        logger.info(f"Starting trade process for {self.trade_id}")
        
        if not await self.initialize():
            logger.error("Failed to initialize, exiting")
            return
        
        try:
            while self.should_run:
                # Получаем актуальное состояние сделки
                trade = shared_state.get_trade(self.trade_id)
                
                if not trade:
                    logger.warning(f"Trade {self.trade_id} not found in shared state")
                    break
                
                # Проверяем, не завершена ли сделка
                if trade.status in [TradePhase.COMPLETED.value]:
                    logger.info(f"Trade {self.trade_id} completed, exiting process")
                    break
                
                # Получаем текущую цену
                current_price = await self._get_current_price(trade.symbol)
                if current_price is None:
                    await self._handle_error("Failed to get current price")
                    continue
                
                # Обрабатываем обновление цены
                await self._process_price_update(trade, current_price)
                
                # Сохраняем состояние если прошло достаточно времени
                await self._periodic_state_save()
                
                # Ждем до следующей проверки
                await asyncio.sleep(self.price_check_interval)
                
        except Exception as e:
            logger.error(f"Unexpected error in trade process: {e}")
        
        finally:
            await self._cleanup()
            logger.info(f"Trade process {self.process_id} for {self.trade_id} finished")

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """Получает текущую цену символа с принудительным обновлением"""
        try:
            # Сначала пробуем получить из shared_state (WebSocket данные)
            market_data = shared_state._system_state.market_data.get(symbol)
            
            # Принудительно получаем свежую цену через REST API каждые 3 сек
            if not market_data or time.time() - market_data.get("timestamp", 0) > 3:
                try:
                    ticker = await self.binance_client.get_symbol_ticker(symbol)
                    if ticker:
                        fresh_price = float(ticker["price"])
                        # Обновляем shared_state с новой ценой
                        shared_state.update_market_data(symbol, fresh_price)
                        logger.debug(f"Updated price for {symbol}: ${fresh_price:.2f}")
                        return fresh_price
                except Exception as e:
                    logger.warning(f"Failed to get fresh price via REST: {e}")
                    
            # Используем кэшированную цену если REST API недоступен
            if market_data and time.time() - market_data.get("timestamp", 0) < 30:
                return market_data["price"]
            
            # Если совсем ничего не работает, пробуем прямой запрос
            price_data = await self.binance_client.get_symbol_price(symbol)
            if price_data:
                return float(price_data["price"])
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting current price: {e}")
            return None

    async def _process_price_update(self, trade: TradeState, current_price: float):
        """Обрабатывает обновление цены и принимает торговые решения"""
        try:
            self.last_price_update = time.time()
            
            # Логируем обновление цены для мониторинга
            entry_price = trade.entry_price
            profit_usd = (current_price - entry_price) * trade.quantity
            logger.debug(f"Price update {trade.symbol}: ${current_price:.2f} "
                        f"(entry: ${entry_price:.2f}, profit: ${profit_usd:.2f})")
            
            # Используем торговую логику для принятия решений
            continue_trade, exit_reason = await trade_logic.update_trade_with_price(
                self.trade_id, current_price
            )
            
            if not continue_trade:
                logger.info(f"Trade logic requested exit: {exit_reason}")
                await self._execute_exit_order(trade, exit_reason)
            
        except Exception as e:
            logger.error(f"Error processing price update: {e}")
            await self._handle_error(f"Price processing error: {e}")

    async def _execute_exit_order(self, trade: TradeState, reason: str):
        """Выполняет ордер на продажу для выхода из сделки"""
        try:
            logger.info(f"Executing exit order for {self.trade_id}, reason: {reason}")
            
            # Создаем рыночный ордер на продажу
            order_result = await self.binance_client.create_market_order(
                symbol=trade.symbol,
                side="SELL",
                quantity=trade.quantity
            )
            
            if order_result:
                # Рассчитываем фактическую прибыль
                if order_result.get("fills") and len(order_result["fills"]) > 0:
                    exit_price = float(order_result["fills"][0]["price"])
                else:
                    exit_price = trade.current_price
                    
                actual_profit = (exit_price - trade.entry_price) * trade.quantity
                
                logger.info(f"Exit order executed: {order_result}")
                logger.info(f"Final P&L: ${actual_profit:.2f}")
                
                # Завершаем сделку в торговой логике
                await trade_logic.complete_trade(self.trade_id, exit_price, actual_profit)
                
                # Останавливаем процесс
                self.should_run = False
                
            else:
                logger.error("Failed to execute exit order")
                await self._handle_error("Exit order execution failed")
                
        except Exception as e:
            logger.error(f"Error executing exit order: {e}")
            await self._handle_error(f"Exit order error: {e}")

    async def _handle_error(self, error_msg: str):
        """Обрабатывает ошибки с подсчетом и логикой завершения"""
        self.error_count += 1
        logger.error(f"Error #{self.error_count}: {error_msg}")
        
        if self.error_count >= self.max_errors:
            logger.critical(f"Too many errors ({self.error_count}), stopping trade process")
            
            # Пытаемся экстренно закрыть позицию
            trade = shared_state.get_trade(self.trade_id)
            if trade:
                try:
                    logger.warning("Attempting emergency exit due to excessive errors")
                    await self._execute_exit_order(trade, "emergency_exit")
                except:
                    logger.critical("Emergency exit failed!")
            
            self.should_run = False
        
        # Увеличиваем интервал проверки после ошибок
        await asyncio.sleep(min(self.error_count * 2, 30))

    async def _periodic_state_save(self):
        """Периодическое сохранение состояния"""
        current_time = time.time()
        if current_time - self.last_state_save > self.state_save_interval:
            shared_state.save_state()
            self.last_state_save = current_time
            logger.debug(f"State saved by process {self.process_id}")

    async def _cleanup(self):
        """Очистка ресурсов при завершении процесса"""
        try:
            # Сохраняем финальное состояние
            shared_state.save_state()
            
            # Закрываем соединения
            if self.binance_client:
                await self.binance_client.close()
            
            # Обновляем информацию о процессе
            shared_state.update_trade(self.trade_id, process_id=None)
            
            logger.info(f"Process {self.process_id} cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


def run_trade_process_sync(trade_id: str, api_key: str, api_secret: str, testnet: bool = True):
    """Синхронная обертка для запуска в multiprocessing"""
    
    # Настройка логирования для процесса
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s - TRADE[{trade_id}] - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f'../logs/trade_{trade_id}_{int(time.time())}.log')
        ]
    )
    
    try:
        # Создаем и запускаем процесс
        process = TradeProcess(trade_id, api_key, api_secret, testnet)
        asyncio.run(process.run())
        
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error in trade process: {e}")
    finally:
        logger.info(f"Trade process for {trade_id} terminated")


class TradeProcessManager:
    """Менеджер для управления процессами сделок"""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.active_processes: Dict[str, multiprocessing.Process] = {}
        
    def start_trade_process(self, trade_id: str) -> bool:
        """Запускает новый процесс для сделки"""
        try:
            if trade_id in self.active_processes:
                logger.warning(f"Process for trade {trade_id} already exists")
                return False
            
            # Создаем процесс
            process = multiprocessing.Process(
                target=run_trade_process_sync,
                args=(trade_id, self.api_key, self.api_secret, self.testnet),
                name=f"trade_process_{trade_id}"
            )
            
            # Запускаем процесс
            process.start()
            self.active_processes[trade_id] = process
            
            logger.info(f"Started trade process for {trade_id}, PID: {process.pid}")
            return True
            
        except Exception as e:
            logger.error(f"Error starting trade process for {trade_id}: {e}")
            return False
    
    def stop_trade_process(self, trade_id: str, timeout: int = 30) -> bool:
        """Останавливает процесс сделки"""
        try:
            if trade_id not in self.active_processes:
                logger.warning(f"No process found for trade {trade_id}")
                return False
            
            process = self.active_processes[trade_id]
            
            if process.is_alive():
                logger.info(f"Terminating trade process for {trade_id}")
                process.terminate()
                process.join(timeout=timeout)
                
                if process.is_alive():
                    logger.warning(f"Force killing process for {trade_id}")
                    process.kill()
                    process.join()
            
            del self.active_processes[trade_id]
            logger.info(f"Trade process for {trade_id} stopped")
            return True
            
        except Exception as e:
            logger.error(f"Error stopping trade process for {trade_id}: {e}")
            return False
    
    def get_process_status(self) -> Dict[str, Any]:
        """Возвращает статус всех процессов"""
        status = {
            "total_processes": len(self.active_processes),
            "active_processes": {},
            "dead_processes": []
        }
        
        for trade_id, process in self.active_processes.copy().items():
            if process.is_alive():
                status["active_processes"][trade_id] = {
                    "pid": process.pid,
                    "name": process.name,
                    "exitcode": process.exitcode
                }
            else:
                status["dead_processes"].append({
                    "trade_id": trade_id,
                    "exitcode": process.exitcode
                })
                # Убираем мертвые процессы
                del self.active_processes[trade_id]
        
        return status
    
    def cleanup_all_processes(self, timeout: int = 30):
        """Останавливает все процессы"""
        logger.info("Cleaning up all trade processes")
        
        for trade_id in list(self.active_processes.keys()):
            self.stop_trade_process(trade_id, timeout)
        
        logger.info("All trade processes cleaned up")