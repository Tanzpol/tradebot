"""
Управление общим состоянием между процессами через JSON файлы
Сохранение каждые 10 секунд + немедленное сохранение критических изменений
"""

import json
import os
import asyncio
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import fcntl  # Для блокировки файлов
import logging

logger = logging.getLogger(__name__)


@dataclass
class TradeState:
    """Состояние одной сделки"""
    trade_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    current_price: float
    entry_time: float
    
    # Профит и трейлинг
    target_profit_usd: float
    current_profit_usd: float
    max_profit_usd: float
    trailing_active: bool
    trailing_threshold: float  # 20% от target_profit_usd
    
    # Стоп-лосс
    stop_loss_price: float
    stop_loss_reason: str  # "commission" или "risk_ratio"
    
    # BNB и комиссии
    bnb_sufficient: bool
    estimated_commission: float
    
    # Статус
    status: str  # "active", "trailing", "closed"
    last_update: float
    process_id: Optional[int] = None


@dataclass
class SystemState:
    """Общее состояние системы"""
    active_trades: Dict[str, TradeState]
    market_data: Dict[str, Any]
    balance_data: Dict[str, float]
    last_update: float
    bot_running: bool
    websocket_connected: bool
    

class SharedState:
    def __init__(self, data_dir: str = "data/state"):
        self.data_dir = data_dir
        self.state_file = os.path.join(data_dir, "system_state.json")
        self.backup_file = os.path.join(data_dir, "system_state.backup.json")
        self.lock_file = os.path.join(data_dir, ".state.lock")
        
        # Создаем папки если не существуют
        os.makedirs(data_dir, exist_ok=True)
        
        # Инициализируем состояние
        self._system_state = SystemState(
            active_trades={},
            market_data={},
            balance_data={},
            last_update=time.time(),
            bot_running=False,
            websocket_connected=False
        )
        
        # Загружаем существующее состояние
        self.load_state()
        
        # Запускаем автосохранение
        self._auto_save_task = None
        self._should_stop = False

    def _acquire_lock(self):
        """Блокируем файл для эксклюзивного доступа"""
        lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd):
        """Освобождаем блокировку"""
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    def save_state(self, force: bool = False):
        """Сохраняет состояние в файл с блокировкой"""
        try:
            # Получаем блокировку
            lock_fd = self._acquire_lock()
            
            try:
                # Создаем резервную копию
                if os.path.exists(self.state_file):
                    with open(self.state_file, 'r') as f:
                        with open(self.backup_file, 'w') as bf:
                            bf.write(f.read())
                
                # Обновляем время
                self._system_state.last_update = time.time()
                
                # Конвертируем состояние в JSON
                state_dict = {
                    "active_trades": {
                        trade_id: asdict(trade_state) 
                        for trade_id, trade_state in self._system_state.active_trades.items()
                    },
                    "market_data": self._system_state.market_data,
                    "balance_data": self._system_state.balance_data,
                    "last_update": self._system_state.last_update,
                    "bot_running": self._system_state.bot_running,
                    "websocket_connected": self._system_state.websocket_connected,
                    "saved_at": datetime.now().isoformat()
                }
                
                # Сохраняем
                with open(self.state_file, 'w') as f:
                    json.dump(state_dict, f, indent=2)
                
                logger.debug(f"State saved at {datetime.now()}")
                
            finally:
                self._release_lock(lock_fd)
                
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def load_state(self):
        """Загружает состояние из файла"""
        try:
            if not os.path.exists(self.state_file):
                logger.info("No existing state file found, starting fresh")
                return
            
            lock_fd = self._acquire_lock()
            
            try:
                with open(self.state_file, 'r') as f:
                    state_dict = json.load(f)
                
                # Восстанавливаем активные сделки
                active_trades = {}
                for trade_id, trade_data in state_dict.get("active_trades", {}).items():
                    active_trades[trade_id] = TradeState(**trade_data)
                
                # Восстанавливаем системное состояние
                self._system_state.active_trades = active_trades
                self._system_state.market_data = state_dict.get("market_data", {})
                self._system_state.balance_data = state_dict.get("balance_data", {})
                self._system_state.last_update = state_dict.get("last_update", time.time())
                self._system_state.bot_running = False  # При перезапуске бот не работает
                self._system_state.websocket_connected = False
                
                logger.info(f"State loaded: {len(active_trades)} active trades")
                
            finally:
                self._release_lock(lock_fd)
                
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            # Пробуем загрузить резервную копию
            self._try_load_backup()

    def _try_load_backup(self):
        """Пробует загрузить резервную копию"""
        try:
            if os.path.exists(self.backup_file):
                logger.warning("Trying to load backup state file")
                with open(self.backup_file, 'r') as f:
                    state_dict = json.load(f)
                
                # Базовое восстановление только активных сделок
                active_trades = {}
                for trade_id, trade_data in state_dict.get("active_trades", {}).items():
                    try:
                        active_trades[trade_id] = TradeState(**trade_data)
                    except Exception as trade_error:
                        logger.error(f"Error restoring trade {trade_id}: {trade_error}")
                
                self._system_state.active_trades = active_trades
                logger.warning(f"Backup loaded: {len(active_trades)} trades restored")
                
        except Exception as e:
            logger.error(f"Error loading backup: {e}")

    # Методы для работы со сделками
    def add_trade(self, trade: TradeState):
        """Добавляет новую сделку"""
        self._system_state.active_trades[trade.trade_id] = trade
        self.save_state(force=True)  # Немедленное сохранение
        logger.info(f"Trade {trade.trade_id} added to state")

    def update_trade(self, trade_id: str, **updates):
        """Обновляет параметры сделки"""
        if trade_id in self._system_state.active_trades:
            trade = self._system_state.active_trades[trade_id]
            for key, value in updates.items():
                if hasattr(trade, key):
                    setattr(trade, key, value)
            trade.last_update = time.time()
            logger.debug(f"Trade {trade_id} updated: {list(updates.keys())}")

    def remove_trade(self, trade_id: str):
        """Удаляет сделку из активных"""
        if trade_id in self._system_state.active_trades:
            del self._system_state.active_trades[trade_id]
            self.save_state(force=True)  # Немедленное сохранение
            logger.info(f"Trade {trade_id} removed from state")

    def get_trade(self, trade_id: str) -> Optional[TradeState]:
        """Получает сделку по ID"""
        return self._system_state.active_trades.get(trade_id)

    def get_all_trades(self) -> Dict[str, TradeState]:
        """Получает все активные сделки"""
        return self._system_state.active_trades.copy()

    def update_market_data(self, symbol: str, price: float, **other_data):
        """Обновляет рыночные данные"""
        self._system_state.market_data[symbol] = {
            "price": price,
            "timestamp": time.time(),
            **other_data
        }

    def update_balance_data(self, balances: Dict[str, float]):
        """Обновляет данные балансов"""
        self._system_state.balance_data.update(balances)

    def set_bot_status(self, running: bool):
        """Устанавливает статус бота"""
        self._system_state.bot_running = running
        self.save_state(force=True)

    def set_websocket_status(self, connected: bool):
        """Устанавливает статус WebSocket"""
        self._system_state.websocket_connected = connected

    # Автосохранение
    async def start_auto_save(self, interval: int = 10):
        """Запускает автосохранение каждые N секунд"""
        self._should_stop = False
        self._auto_save_task = asyncio.create_task(self._auto_save_loop(interval))

    async def stop_auto_save(self):
        """Останавливает автосохранение"""
        self._should_stop = True
        if self._auto_save_task:
            self._auto_save_task.cancel()

    async def _auto_save_loop(self, interval: int):
        """Цикл автосохранения"""
        while not self._should_stop:
            try:
                await asyncio.sleep(interval)
                if not self._should_stop:
                    self.save_state()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in auto-save loop: {e}")

    # Получение системной информации
    def get_system_info(self) -> Dict[str, Any]:
        """Получает общую информацию о системе"""
        return {
            "active_trades_count": len(self._system_state.active_trades),
            "bot_running": self._system_state.bot_running,
            "websocket_connected": self._system_state.websocket_connected,
            "last_update": self._system_state.last_update,
            "uptime_seconds": time.time() - self._system_state.last_update,
            "current_btc_price": self._system_state.market_data.get("BTCUSDT", {}).get("price"),
            "available_balance": self._system_state.balance_data.get("USDT", 0)
        }


# Глобальный экземпляр для использования в других модулях
shared_state = SharedState()