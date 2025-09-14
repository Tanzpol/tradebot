"""
Telegram уведомления для торгового бота
- Уведомления о входе/выходе из сделок
- Критические ошибки API
- Статистика производительности
"""

import asyncio
import logging
import os
from typing import Optional, Dict, Any
import aiohttp
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self.enabled = bool(self.bot_token and self.chat_id)
        self.session: Optional[aiohttp.ClientSession] = None
        
        if not self.enabled:
            logger.warning("Telegram notifications disabled: missing bot_token or chat_id")
        else:
            logger.info("Telegram notifications enabled")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получает HTTP сессию"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self):
        """Закрывает HTTP сессию"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправляет сообщение в Telegram"""
        if not self.enabled:
            logger.debug(f"Telegram notification skipped: {text[:100]}...")
            return False

        try:
            session = await self._get_session()
            url = f"{self.base_url}/sendMessage"
            
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            
            async with session.post(url, data=data) as response:
                if response.status == 200:
                    logger.debug("Telegram notification sent successfully")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Telegram API error {response.status}: {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return False

    async def notify_bot_started(self):
        """Уведомление о запуске бота"""
        message = f"""
🤖 <b>Trading Bot v2 Started</b>

📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔧 Mode: {'Testnet' if os.getenv('BINANCE_TESTNET', 'true').lower() == 'true' else 'Production'}
💰 Target Profit: ${os.getenv('TARGET_PROFIT_USD', '50')}
📊 Max Concurrent Trades: {os.getenv('MAX_CONCURRENT_TRADES', '10')}

Bot is now monitoring the market for entry signals.
        """
        await self.send_message(message.strip())

    async def notify_bot_stopped(self):
        """Уведомление об остановке бота"""
        message = f"""
🛑 <b>Trading Bot v2 Stopped</b>

📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Bot has stopped monitoring the market.
        """
        await self.send_message(message.strip())

    async def notify_trade_entry(self, trade_info: Dict[str, Any]):
        """Уведомление о входе в сделку"""
        message = f"""
📈 <b>New Trade Opened</b>

🪙 Symbol: {trade_info.get('symbol', 'N/A')}
💵 Entry Price: ${trade_info.get('entry_price', 0):.2f}
📊 Quantity: {trade_info.get('quantity', 0):.6f}
💰 Trade Amount: ${trade_info.get('trade_amount_usd', 0):.2f}
🎯 Target Profit: ${trade_info.get('target_profit_usd', 0):.2f}
🔴 Stop Loss: ${trade_info.get('stop_loss_price', 0):.2f}

📋 Trade ID: <code>{trade_info.get('trade_id', 'N/A')}</code>
        """
        await self.send_message(message.strip())

    async def notify_trade_exit(self, trade_info: Dict[str, Any], reason: str, profit: float):
        """Уведомление о выходе из сделки"""
        profit_emoji = "💚" if profit > 0 else "❌" if profit < 0 else "⚪"
        reason_text = {
            "take_profit": "🎯 Take Profit",
            "stop_loss": "🔴 Stop Loss",
            "trailing_stop": "📈 Trailing Stop",
            "profit_protection": "🛡️ Profit Protection",
            "manual": "👤 Manual Exit",
            "emergency_exit": "🚨 Emergency Exit"
        }.get(reason, f"📤 {reason}")
        
        message = f"""
{profit_emoji} <b>Trade Closed</b>

🪙 Symbol: {trade_info.get('symbol', 'N/A')}
📋 Trade ID: <code>{trade_info.get('trade_id', 'N/A')}</code>

💵 Entry Price: ${trade_info.get('entry_price', 0):.2f}
💵 Exit Price: ${trade_info.get('exit_price', trade_info.get('current_price', 0)):.2f}
📊 Quantity: {trade_info.get('quantity', 0):.6f}

💰 P&L: <b>${profit:.2f}</b>
📈 ROI: {(profit / (trade_info.get('entry_price', 1) * trade_info.get('quantity', 1))) * 100:.2f}%

🔄 Reason: {reason_text}
⏱️ Duration: {self._format_duration(trade_info.get('duration_minutes', 0))}
        """
        await self.send_message(message.strip())

    async def notify_trailing_activated(self, trade_info: Dict[str, Any]):
        """Уведомление об активации трейлинга"""
        message = f"""
📈 <b>Trailing Stop Activated</b>

🪙 Symbol: {trade_info.get('symbol', 'N/A')}
📋 Trade ID: <code>{trade_info.get('trade_id', 'N/A')}</code>

💰 Current Profit: ${trade_info.get('current_profit_usd', 0):.2f}
🎯 Target Reached: ${trade_info.get('target_profit_usd', 0):.2f}

The bot will now follow the price and exit when it drops by {trade_info.get('trailing_percent', 20)}% from the peak.
        """
        await self.send_message(message.strip())

    async def notify_api_error(self, error_type: str, error_message: str, trade_id: Optional[str] = None):
        """Уведомление о критической ошибке API"""
        message = f"""
🚨 <b>Critical API Error</b>

⚠️ Type: {error_type}
💬 Message: {error_message}
📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
        if trade_id:
            message += f"\n📋 Trade ID: <code>{trade_id}</code>"
        
        message += "\n\n⚡ Immediate attention required!"
        
        await self.send_message(message.strip())

    async def notify_low_balance(self, asset: str, balance: float, threshold: float):
        """Уведомление о низком балансе"""
        message = f"""
⚠️ <b>Low Balance Alert</b>

💰 Asset: {asset}
📊 Current Balance: {balance:.6f}
🚨 Threshold: {threshold:.6f}

Consider topping up your account or adjusting trade sizes.
        """
        await self.send_message(message.strip())

    async def notify_bnb_insufficient(self, required: float, available: float):
        """Уведомление о недостаточном количестве BNB"""
        message = f"""
⚠️ <b>Insufficient BNB for Discount</b>

🟡 Required: {required:.6f} BNB
💰 Available: {available:.6f} BNB

Trading fees will be higher (0.1% vs 0.075%). Consider buying more BNB.
        """
        await self.send_message(message.strip())

    async def notify_daily_summary(self, summary: Dict[str, Any]):
        """Ежедневная сводка торговли"""
        total_trades = summary.get('total_trades', 0)
        profitable_trades = summary.get('profitable_trades', 0)
        total_profit = summary.get('total_profit_usd', 0)
        win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
        
        profit_emoji = "💚" if total_profit > 0 else "❌" if total_profit < 0 else "⚪"
        
        message = f"""
📊 <b>Daily Trading Summary</b>
📅 {datetime.now().strftime('%Y-%m-%d')}

📈 Total Trades: {total_trades}
✅ Profitable: {profitable_trades}
❌ Losses: {total_trades - profitable_trades}
🎯 Win Rate: {win_rate:.1f}%

{profit_emoji} <b>Total P&L: ${total_profit:.2f}</b>

💰 Best Trade: ${summary.get('best_trade', 0):.2f}
💸 Worst Trade: ${summary.get('worst_trade', 0):.2f}
⏱️ Avg Duration: {self._format_duration(summary.get('avg_duration_minutes', 0))}
        """
        await self.send_message(message.strip())

    async def notify_system_status(self, status: Dict[str, Any]):
        """Уведомление о статусе системы"""
        ws_status = "✅ Connected" if status.get('websocket_connected') else "❌ Disconnected"
        bot_status = "🟢 Running" if status.get('bot_running') else "🔴 Stopped"
        
        message = f"""
🖥️ <b>System Status Check</b>

🤖 Bot: {bot_status}
🔌 WebSocket: {ws_status}
📊 Active Trades: {status.get('active_trades', 0)}
💰 Available Balance: ${status.get('available_balance', 0):.2f}
📈 BTC Price: ${status.get('current_btc_price', 0):.2f}

⏰ Uptime: {self._format_duration(status.get('uptime_minutes', 0))}
        """
        await self.send_message(message.strip())

    def _format_duration(self, minutes: float) -> str:
        """Форматирует длительность в читаемый вид"""
        if minutes < 60:
            return f"{int(minutes)}m"
        elif minutes < 1440:  # 24 hours
            hours = int(minutes // 60)
            mins = int(minutes % 60)
            return f"{hours}h {mins}m"
        else:
            days = int(minutes // 1440)
            hours = int((minutes % 1440) // 60)
            return f"{days}d {hours}h"

    async def test_connection(self) -> bool:
        """Тестирует подключение к Telegram API"""
        if not self.enabled:
            return False
        
        try:
            test_message = f"🧪 Trading Bot v2 - Connection Test\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            return await self.send_message(test_message)
        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}")
            return False


# Глобальный экземпляр уведомлений
telegram_notifier = TelegramNotifier()