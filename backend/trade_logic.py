"""
Основная торговая логика с нашими правилами:
1. Покупка BTC при сигнале
2. Ожидание профита $50 для активации трейлинга
3. Трейлинг с отступом 20% от профита ($10)
4. Защита от "съедания" профита
5. Стоп-лосс по формуле max(комиссии, 50% профита)
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Tuple, Any
from enum import Enum
from dataclasses import dataclass
from shared_state import shared_state, TradeState
from risk_calculator import risk_calculator

logger = logging.getLogger(__name__)


class TradePhase(Enum):
    """Фазы жизненного цикла сделки"""
    ENTERING = "entering"          # Создание ордера на покупку
    WAITING_PROFIT = "waiting"     # Ожидание достижения целевого профита
    TRAILING = "trailing"          # Активный трейлинг
    EXITING = "exiting"           # Создание ордера на продажу
    COMPLETED = "completed"        # Сделка завершена


@dataclass
class TradeSignal:
    """Сигнал для создания сделки"""
    symbol: str
    entry_price: float
    target_profit_usd: float
    confidence: float
    reasons: list
    timestamp: float


class TradeLogic:
    def __init__(self):
        self.target_profit_usd = 50.0
        self.trailing_percent = 20.0  # 20% от профита = $10
        self.phase_callbacks = {}
        
    def set_phase_callback(self, phase: TradePhase, callback):
        """Устанавливает callback для определенной фазы"""
        self.phase_callbacks[phase] = callback

    async def analyze_entry_signal(
        self, 
        symbol: str, 
        current_price: float, 
        market_indicators: Dict[str, Any],
        available_balance: float
    ) -> Optional[TradeSignal]:
        """
        Анализирует условия для входа в сделку
        Здесь будет ваша логика принятия решений
        """
        
        # Пример простой логики - можете расширить
        rsi = market_indicators.get("rsi", 50)
        macd = market_indicators.get("macd", 0)
        macd_signal = market_indicators.get("macdsignal", 0)
        
        reasons = []
        score = 0
        
        # RSI условия
        if rsi < 35:
            score += 30
            reasons.append(f"RSI oversold: {rsi:.1f}")
        elif rsi < 45:
            score += 15
            reasons.append(f"RSI favorable: {rsi:.1f}")
        
        # MACD условия
        if macd > macd_signal and macd > 0:
            score += 25
            reasons.append("MACD bullish crossover")
        elif macd > macd_signal:
            score += 10
            reasons.append("MACD positive momentum")
        
        # Проверка минимального баланса
        if available_balance < 50:  # Минимум для нашей стратегии
            return None
        
        # Требуем минимальный score для входа
        if score < 30:
            return None
        
        confidence = min(score / 60.0, 1.0)  # Нормализуем score в confidence
        
        return TradeSignal(
            symbol=symbol,
            entry_price=current_price,
            target_profit_usd=self.target_profit_usd,
            confidence=confidence,
            reasons=reasons,
            timestamp=time.time()
        )

    async def create_trade_state(
        self, 
        signal: TradeSignal, 
        trade_amount_usd: float,
        bnb_balance: float = 0,
        bnb_price: float = 300.0
    ) -> Tuple[bool, TradeState, str]:
        """
        Создает состояние новой сделки с проверкой рисков
        """
        
        # Проверяем жизнеспособность сделки
        is_viable, reason, risk_details = risk_calculator.check_trade_viability(
            trade_amount_usd=trade_amount_usd,
            target_profit_usd=signal.target_profit_usd,
            entry_price=signal.entry_price,
            current_bnb_balance=bnb_balance,
            bnb_price_usd=bnb_price
        )
        
        if not is_viable:
            return False, None, f"Trade not viable: {reason}"
        
        # Рассчитываем параметры сделки
        btc_quantity = trade_amount_usd / signal.entry_price
        trailing_threshold = signal.target_profit_usd * (self.trailing_percent / 100)
        
        # Создаем уникальный ID сделки
        trade_id = f"trade_{int(signal.timestamp * 1000)}"
        
        # Создаем состояние сделки
        trade_state = TradeState(
            trade_id=trade_id,
            symbol=signal.symbol,
            side="BUY",
            quantity=btc_quantity,
            entry_price=signal.entry_price,
            current_price=signal.entry_price,
            entry_time=signal.timestamp,
            
            # Профит и трейлинг
            target_profit_usd=signal.target_profit_usd,
            current_profit_usd=0.0,
            max_profit_usd=0.0,
            trailing_active=False,
            trailing_threshold=trailing_threshold,
            
            # Стоп-лосс
            stop_loss_price=risk_details["stop_loss"].stop_loss_price,
            stop_loss_reason=risk_details["stop_loss"].reason,
            
            # BNB и комиссии
            bnb_sufficient=risk_details["bnb_requirement"].is_sufficient,
            estimated_commission=risk_details["commissions"].total_commission_usd,
            
            # Статус
            status=TradePhase.ENTERING.value,
            last_update=time.time()
        )
        
        # Логируем отчет о рисках
        logger.info(f"Creating new trade {trade_id}")
        risk_report = risk_calculator.format_risk_report(trade_amount_usd, risk_details)
        logger.info(risk_report)
        
        return True, trade_state, "Trade state created successfully"

    async def update_trade_with_price(self, trade_id: str, current_price: float) -> Tuple[bool, Optional[str]]:
        """
        Обновляет сделку новой ценой и принимает решения
        
        Returns:
            (continue_trade, exit_reason)
        """
        
        trade = shared_state.get_trade(trade_id)
        if not trade:
            return False, "Trade not found"
        
        # Обновляем текущую цену
        trade.current_price = current_price
        
        # Рассчитываем текущий профит
        profit_per_btc = current_price - trade.entry_price
        current_profit_usd = profit_per_btc * trade.quantity
        trade.current_profit_usd = current_profit_usd
        
        # Проверяем стоп-лосс (всегда в приоритете!)
        if current_price <= trade.stop_loss_price:
            logger.info(f"Stop loss triggered for {trade_id}: {current_price} <= {trade.stop_loss_price}")
            await self._trigger_exit(trade, "stop_loss")
            return False, "stop_loss"
        
        # Определяем фазу сделки
        current_phase = TradePhase(trade.status)
        
        if current_phase == TradePhase.WAITING_PROFIT:
            return await self._handle_waiting_phase(trade, current_profit_usd)
        
        elif current_phase == TradePhase.TRAILING:
            return await self._handle_trailing_phase(trade, current_profit_usd)
        
        # Обновляем состояние в shared_state
        shared_state.update_trade(trade_id, 
                                current_price=current_price,
                                current_profit_usd=current_profit_usd,
                                last_update=time.time())
        
        return True, None

    async def _handle_waiting_phase(self, trade: TradeState, current_profit_usd: float) -> Tuple[bool, Optional[str]]:
        """Обрабатывает фазу ожидания профита"""
        
        # Проверяем достижение целевого профита
        if current_profit_usd >= trade.target_profit_usd:
            logger.info(f"Target profit reached for {trade.trade_id}: ${current_profit_usd:.2f}")
            
            # Активируем трейлинг
            trade.trailing_active = True
            trade.max_profit_usd = current_profit_usd
            trade.status = TradePhase.TRAILING.value
            
            # Сохраняем изменения
            shared_state.update_trade(trade.trade_id,
                                    trailing_active=True,
                                    max_profit_usd=current_profit_usd,
                                    status=TradePhase.TRAILING.value)
            
            logger.info(f"Trailing activated for {trade.trade_id} at ${current_profit_usd:.2f} profit")
            
            # Вызываем callback если есть
            if TradePhase.TRAILING in self.phase_callbacks:
                await self.phase_callbacks[TradePhase.TRAILING](trade)
        
        return True, None

    async def _handle_trailing_phase(self, trade: TradeState, current_profit_usd: float) -> Tuple[bool, Optional[str]]:
        """Обрабатывает фазу трейлинга"""
        
        # Обновляем максимальный профит если нужно
        if current_profit_usd > trade.max_profit_usd:
            trade.max_profit_usd = current_profit_usd
            shared_state.update_trade(trade.trade_id, max_profit_usd=current_profit_usd)
            logger.debug(f"New max profit for {trade.trade_id}: ${current_profit_usd:.2f}")
        
        # Рассчитываем уровни для выхода
        trailing_exit_level = trade.max_profit_usd - trade.trailing_threshold
        minimum_acceptable_profit = trade.target_profit_usd - (trade.trailing_threshold * 0.5)  # Защита от съедания
        
        # Условие 1: Профит упал на trailing_threshold от максимума
        profit_dropped_enough = current_profit_usd <= trailing_exit_level
        
        # Условие 2: Профит приближается к изначальному целевому (защита от съедания)
        profit_being_eaten = current_profit_usd <= minimum_acceptable_profit
        
        if profit_dropped_enough or profit_being_eaten:
            reason = "trailing_stop" if profit_dropped_enough else "profit_protection"
            
            logger.info(f"Trailing exit triggered for {trade.trade_id}: "
                       f"current=${current_profit_usd:.2f}, max=${trade.max_profit_usd:.2f}, "
                       f"threshold=${trade.trailing_threshold:.2f}, reason={reason}")
            
            await self._trigger_exit(trade, reason)
            return False, reason
        
        return True, None

    async def _trigger_exit(self, trade: TradeState, reason: str):
        """Инициирует процедуру выхода из сделки"""
        
        trade.status = TradePhase.EXITING.value
        shared_state.update_trade(trade.trade_id, status=TradePhase.EXITING.value)
        
        logger.info(f"Exit triggered for {trade.trade_id}: {reason}")
        logger.info(f"Final stats - Entry: ${trade.entry_price:.2f}, Current: ${trade.current_price:.2f}, "
                   f"Profit: ${trade.current_profit_usd:.2f}, Max Profit: ${trade.max_profit_usd:.2f}")
        
        # Вызываем callback для выхода
        if TradePhase.EXITING in self.phase_callbacks:
            await self.phase_callbacks[TradePhase.EXITING](trade, reason)

    async def complete_trade(self, trade_id: str, exit_price: float, actual_profit: float):
        """Завершает сделку после выполнения ордера на продажу"""
        
        trade = shared_state.get_trade(trade_id)
        if not trade:
            return
        
        trade.status = TradePhase.COMPLETED.value
        trade.current_price = exit_price
        trade.current_profit_usd = actual_profit
        
        # Сохраняем финальное состояние
        shared_state.update_trade(trade_id,
                                status=TradePhase.COMPLETED.value,
                                current_price=exit_price,
                                current_profit_usd=actual_profit,
                                last_update=time.time())
        
        # Удаляем из активных сделок
        shared_state.remove_trade(trade_id)
        
        logger.info(f"Trade {trade_id} completed: Entry=${trade.entry_price:.2f}, "
                   f"Exit=${exit_price:.2f}, Profit=${actual_profit:.2f}")
        
        # Вызываем callback завершения
        if TradePhase.COMPLETED in self.phase_callbacks:
            await self.phase_callbacks[TradePhase.COMPLETED](trade)

    def get_trade_summary(self, trade_id: str) -> Dict[str, Any]:
        """Получает сводку по сделке для мониторинга"""
        
        trade = shared_state.get_trade(trade_id)
        if not trade:
            return {"error": "Trade not found"}
        
        current_phase = TradePhase(trade.status)
        profit_percent = (trade.current_profit_usd / (trade.entry_price * trade.quantity)) * 100
        
        summary = {
            "trade_id": trade_id,
            "symbol": trade.symbol,
            "phase": current_phase.value,
            "entry_price": trade.entry_price,
            "current_price": trade.current_price,
            "quantity": trade.quantity,
            "current_profit_usd": trade.current_profit_usd,
            "profit_percent": profit_percent,
            "target_profit": trade.target_profit_usd,
            "max_profit_achieved": trade.max_profit_usd,
            "trailing_active": trade.trailing_active,
            "stop_loss_price": trade.stop_loss_price,
            "age_minutes": (time.time() - trade.entry_time) / 60
        }
        
        # Дополнительная информация в зависимости от фазы
        if current_phase == TradePhase.WAITING_PROFIT:
            summary["progress_to_target"] = (trade.current_profit_usd / trade.target_profit_usd) * 100
        
        elif current_phase == TradePhase.TRAILING:
            summary["trailing_buffer"] = trade.max_profit_usd - trade.current_profit_usd
            summary["exit_threshold"] = trade.max_profit_usd - trade.trailing_threshold
        
        return summary

    def get_all_trades_summary(self) -> Dict[str, Any]:
        """Получает сводку по всем активным сделкам"""
        
        all_trades = shared_state.get_all_trades()
        
        summary = {
            "total_active_trades": len(all_trades),
            "total_unrealized_profit": 0,
            "phases_distribution": {},
            "trades": {}
        }
        
        for trade_id, trade in all_trades.items():
            trade_summary = self.get_trade_summary(trade_id)
            summary["trades"][trade_id] = trade_summary
            
            # Суммируем нереализованную прибыль
            summary["total_unrealized_profit"] += trade.current_profit_usd
            
            # Считаем распределение по фазам
            phase = trade.status
            summary["phases_distribution"][phase] = summary["phases_distribution"].get(phase, 0) + 1
        
        return summary


# Глобальный экземпляр торговой логики
trade_logic = TradeLogic()