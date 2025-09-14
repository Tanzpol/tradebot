"""
Binance WebSocket клиент для получения данных в реальном времени:
- Цены тикеров с MAINNET (работает стабильно)
- User Data Stream с testnet/mainnet (по настройке)
- Автоматическое переподключение

ИСПРАВЛЕНИЯ:
- WebSocket для цен ВСЕГДА с mainnet (публичные данные)
- REST API остается testnet/mainnet как настроено
"""

import asyncio
import json
import logging
import time
import websockets
from typing import Optional, Callable, Dict, Any
from websockets.exceptions import ConnectionClosed, WebSocketException
import requests
from shared_state import shared_state

logger = logging.getLogger(__name__)


class BinanceWebSocketClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        # WebSocket для цен ВСЕГДА mainnet (публичные данные)
        self.ws_base = "wss://stream.binance.com:9443/ws/"
        
        # REST API согласно настройке testnet/mainnet
        if testnet:
            self.api_base = "https://testnet.binance.vision"
        else:
            self.api_base = "https://api.binance.com"
        
        # Состояние подключений
        self.price_ws = None
        self.user_ws = None
        self.listen_key = None
        
        # Callback функции
        self.price_callback: Optional[Callable] = None
        self.balance_callback: Optional[Callable] = None
        self.order_callback: Optional[Callable] = None
        
        # Контроль переподключения
        self.should_run = False
        self.reconnect_attempts = 5
        self.reconnect_delay = 5
        
        # Задачи для фонового выполнения
        self.price_task = None
        self.keepalive_task = None

    async def start(self, symbol: str = "BTCUSDC"):
        """Запуск WebSocket клиента"""
        self.should_run = True
        logger.info(f"Starting WebSocket client for {symbol}")
        logger.info(f"Price WebSocket: MAINNET, REST API: {'TESTNET' if self.testnet else 'MAINNET'}")
        
        # Запускаем поток цен
        self.price_task = asyncio.create_task(self.price_stream(symbol))
        self.keepalive_task = asyncio.create_task(self.keepalive_listen_key())
        
        # Обновляем статус в shared state
        shared_state.set_websocket_status(self.price_ws is not None)
        
        logger.info("WebSocket client started")

    async def stop(self):
        """Остановка WebSocket клиента"""
        logger.info("Stopping WebSocket client")
        self.should_run = False
        
        # Отменяем задачи
        if self.price_task:
            self.price_task.cancel()
        if self.keepalive_task:
            self.keepalive_task.cancel()
        
        # Закрываем подключения
        if self.price_ws:
            await self.price_ws.close()
        if self.user_ws:
            await self.user_ws.close()
        
        # Удаляем listen key
        await self.delete_listen_key()
        
        # Обновляем статус
        shared_state.set_websocket_status(False)
        
        logger.info("WebSocket client stopped")

    async def get_listen_key(self):
        """Получение listen key для user data stream"""
        try:
            url = f"{self.api_base}/api/v3/userDataStream"
            headers = {"X-MBX-APIKEY": self.api_key}
            
            response = requests.post(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                self.listen_key = data["listenKey"]
                logger.info("Listen key obtained")
                return True
            else:
                logger.error(f"Failed to get listen key: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error getting listen key: {e}")
            return False

    async def delete_listen_key(self):
        """Удаление listen key"""
        if not self.listen_key:
            return
            
        try:
            url = f"{self.api_base}/api/v3/userDataStream"
            headers = {"X-MBX-APIKEY": self.api_key}
            data = {"listenKey": self.listen_key}
            
            response = requests.delete(url, headers=headers, json=data, timeout=10)
            if response.status_code == 200:
                logger.info("Listen key deleted")
            else:
                logger.warning(f"Failed to delete listen key: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Error deleting listen key: {e}")

    async def keepalive_listen_key(self):
        """Поддержание listen key активным (каждые 30 минут)"""
        while self.should_run:
            try:
                await asyncio.sleep(30 * 60)  # 30 минут
                
                if not self.listen_key:
                    continue
                
                url = f"{self.api_base}/api/v3/userDataStream"
                headers = {"X-MBX-APIKEY": self.api_key}
                data = {"listenKey": self.listen_key}
                
                response = requests.put(url, headers=headers, json=data, timeout=10)
                if response.status_code == 200:
                    logger.debug("Listen key renewed")
                else:
                    logger.warning(f"Failed to renew listen key: {response.status_code}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in keepalive: {e}")

    async def price_stream(self, symbol: str):
        """Поток цен тикера - ВСЕГДА MAINNET"""
        url = f"{self.ws_base}{symbol.lower()}@ticker"
        reconnect_count = 0
        
        while self.should_run:
            try:
                logger.info(f"Connecting to MAINNET price stream: {url}")
                
                async with websockets.connect(url) as ws:
                    self.price_ws = ws
                    reconnect_count = 0  # Сбрасываем счетчик при успешном подключении
                    logger.info(f"✅ MAINNET price stream connected for {symbol}")
                    
                    # Отправляем ping каждые 20 секунд
                    ping_task = asyncio.create_task(self.ping_websocket(ws, 20))
                    
                    try:
                        async for message in ws:
                            if not self.should_run:
                                break
                            
                            try:
                                data = json.loads(message)
                                await self.handle_price_data(data)
                            except json.JSONDecodeError as e:
                                logger.warning(f"Invalid JSON in price stream: {e}")
                                
                    finally:
                        ping_task.cancel()
                
            except (ConnectionClosed, WebSocketException) as e:
                if self.should_run:
                    reconnect_count += 1
                    logger.warning(f"Price stream disconnected (attempt {reconnect_count}): {e}")
                    
                    if reconnect_count >= self.reconnect_attempts:
                        logger.error("Max reconnect attempts reached for price stream")
                        break
                    
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    break
                    
            except Exception as e:
                logger.error(f"Unexpected error in price stream: {e}")
                break

    async def user_data_stream(self):
        """Поток пользовательских данных (баланс, ордера)"""
        if not self.listen_key:
            logger.error("No listen key for user data stream")
            return
        
        if self.testnet:
            url = f"wss://testnet.binance.vision/ws/{self.listen_key}"
        else:
            url = f"wss://stream.binance.com:9443/ws/{self.listen_key}"
        
        reconnect_count = 0
        
        while self.should_run:
            try:
                logger.info(f"Connecting to user data stream")
                
                async with websockets.connect(url) as ws:
                    self.user_ws = ws
                    reconnect_count = 0
                    logger.info("User data stream connected")
                    
                    # Отправляем ping каждые 20 секунд
                    ping_task = asyncio.create_task(self.ping_websocket(ws, 20))
                    
                    try:
                        async for message in ws:
                            if not self.should_run:
                                break
                            
                            try:
                                data = json.loads(message)
                                await self.handle_user_data(data)
                            except json.JSONDecodeError as e:
                                logger.warning(f"Invalid JSON in user stream: {e}")
                                
                    finally:
                        ping_task.cancel()
                
            except (ConnectionClosed, WebSocketException) as e:
                if self.should_run:
                    reconnect_count += 1
                    logger.warning(f"User stream disconnected (attempt {reconnect_count}): {e}")
                    
                    if reconnect_count >= self.reconnect_attempts:
                        logger.error("Max reconnect attempts reached for user stream")
                        break
                    
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    break
                    
            except Exception as e:
                logger.error(f"Unexpected error in user stream: {e}")
                break

    async def ping_websocket(self, ws, interval: int):
        """Отправка ping для поддержания соединения"""
        try:
            while self.should_run:
                await asyncio.sleep(interval)
                if not self.should_run:
                    break
                
                await ws.ping()
                logger.debug("WebSocket ping sent")
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Ping error: {e}")

    async def handle_price_data(self, data: Dict[str, Any]):
        """Обработка данных цен"""
        try:
            if "c" in data:  # close price
                symbol = data.get("s", "UNKNOWN")
                price = float(data["c"])
                timestamp = data.get("E", time.time() * 1000) / 1000
                
                # Сохраняем в shared state
                shared_state.update_market_data(
                    symbol=symbol,
                    price=price,
                    bid=float(data.get("b", 0)),
                    ask=float(data.get("a", 0)),
                    volume=float(data.get("v", 0)),
                    change_24h=float(data.get("P", 0)),
                    timestamp=timestamp
                )
                
                # Вызываем callback если есть
                if self.price_callback:
                    await self.price_callback(symbol, price, data)
                
                logger.debug(f"Price update [MAINNET]: {symbol} = {price}")
                
        except Exception as e:
            logger.error(f"Error handling price data: {e}")

    async def handle_user_data(self, data: Dict[str, Any]):
        """Обработка пользовательских данных"""
        try:
            event_type = data.get("e")
            
            if event_type == "outboundAccountPosition":
                # Обновление баланса
                await self.handle_balance_update(data)
                
            elif event_type == "executionReport":
                # Обновление ордера
                await self.handle_order_update(data)
                
            else:
                logger.debug(f"Unknown user data event: {event_type}")
                
        except Exception as e:
            logger.error(f"Error handling user data: {e}")

    async def handle_balance_update(self, data: Dict[str, Any]):
        """Обработка обновления баланса"""
        try:
            balances = {}
            
            for balance in data.get("B", []):
                asset = balance.get("a")
                free = float(balance.get("f", 0))
                locked = float(balance.get("l", 0))
                
                if free > 0 or locked > 0:
                    balances[asset] = {
                        "free": free,
                        "locked": locked,
                        "total": free + locked
                    }
            
            # Обновляем shared state
            balance_totals = {asset: info["total"] for asset, info in balances.items()}
            shared_state.update_balance_data(balance_totals)
            
            # Вызываем callback
            if self.balance_callback:
                await self.balance_callback(balances)
            
            logger.debug(f"Balance update: {len(balances)} assets")
            
        except Exception as e:
            logger.error(f"Error handling balance update: {e}")

    async def handle_order_update(self, data: Dict[str, Any]):
        """Обработка обновления ордера"""
        try:
            order_info = {
                "symbol": data.get("s"),
                "order_id": data.get("i"),
                "client_order_id": data.get("c"),
                "side": data.get("S"),
                "order_type": data.get("o"),
                "status": data.get("X"),
                "quantity": float(data.get("q", 0)),
                "filled_quantity": float(data.get("z", 0)),
                "price": float(data.get("p", 0)),
                "avg_price": float(data.get("ap", 0)) if data.get("ap") != "0.00000000" else 0,
                "timestamp": data.get("E", time.time() * 1000) / 1000
            }
            
            # Вызываем callback
            if self.order_callback:
                await self.order_callback(order_info)
            
            logger.info(f"Order update: {order_info['symbol']} {order_info['side']} "
                       f"{order_info['status']} - {order_info['filled_quantity']}/{order_info['quantity']}")
            
        except Exception as e:
            logger.error(f"Error handling order update: {e}")

    # Методы для установки callback функций
    def set_price_callback(self, callback: Callable):
        """Устанавливает callback для обновления цен"""
        self.price_callback = callback

    def set_balance_callback(self, callback: Callable):
        """Устанавливает callback для обновления баланса"""
        self.balance_callback = callback

    def set_order_callback(self, callback: Callable):
        """Устанавливает callback для обновления ордеров"""
        self.order_callback = callback

    def is_connected(self) -> bool:
        """Проверяет статус подключения"""
        try:
            return (self.price_ws is not None and 
                    hasattr(self.price_ws, 'close_code') and 
                    self.price_ws.close_code is None)
        except:
            # Простая проверка если что-то не работает
            return self.price_ws is not None and self.should_run

    def get_status(self) -> Dict[str, Any]:
        """Получает статус WebSocket клиента"""
        return {
            "should_run": self.should_run,
            "price_connected": self.price_ws is not None and not self.price_ws.closed,
            "user_connected": self.user_ws is not None and not self.user_ws.closed,
            "has_listen_key": self.listen_key is not None,
            "listen_key": self.listen_key,
            "price_source": "MAINNET",
            "api_source": "TESTNET" if self.testnet else "MAINNET"
        }