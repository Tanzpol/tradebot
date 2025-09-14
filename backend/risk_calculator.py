"""
Расчет рисков, комиссий и требований к BNB
Реализует нашу логику:
from typing import Dict, Tuple, Optional, Any, Any
- Стоп-лосс = max(комиссии, 50% от целевого профита)  
- Проверка BNB с коэффициентом 2.5
- Расчет комиссий с учетом BNB скидки (0.075% vs 0.1%)
"""

import logging
from typing import Dict, Tuple, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CommissionCalculation:
    """Результат расчета комиссий"""
    entry_commission_usd: float
    exit_commission_usd: float
    total_commission_usd: float
    entry_commission_bnb: float
    exit_commission_bnb: float
    total_commission_bnb: float
    has_bnb_discount: bool
    commission_rate_used: float


@dataclass
class StopLossCalculation:
    """Результат расчета стоп-лосса"""
    stop_loss_price: float
    stop_loss_amount_usd: float
    reason: str  # "commission_based" или "profit_ratio_based"
    commission_amount: float
    profit_ratio_amount: float


@dataclass
class BNBRequirement:
    """Требования к BNB"""
    required_bnb: float
    required_bnb_with_safety: float
    current_bnb_balance: float
    is_sufficient: bool
    bnb_price_usd: float
    safety_multiplier: float


class RiskCalculator:
    def __init__(self):
        # Комиссии Binance
        self.commission_rate_standard = 0.001  # 0.1%
        self.commission_rate_with_bnb = 0.00075  # 0.075%
        
        # Настройки из конфига (можно вынести в config.json)
        self.stop_loss_profit_ratio = 0.5  # 50% от целевого профита
        self.bnb_safety_multiplier = 2.5
        
    def calculate_commissions(
        self, 
        trade_amount_usd: float, 
        bnb_balance: float = 0,
        bnb_price_usd: float = 300.0
    ) -> CommissionCalculation:
        """
        Рассчитывает комиссии для сделки
        
        Args:
            trade_amount_usd: Сумма сделки в USD
            bnb_balance: Текущий баланс BNB
            bnb_price_usd: Цена BNB в USD
        """
        
        # Определяем, есть ли достаточно BNB для скидки
        commission_rate = self.commission_rate_with_bnb if bnb_balance > 0.001 else self.commission_rate_standard
        has_bnb_discount = bnb_balance > 0.001
        
        # Расчет комиссий в USD
        entry_commission_usd = trade_amount_usd * commission_rate
        exit_commission_usd = trade_amount_usd * commission_rate  # Приблизительно равна entry
        total_commission_usd = entry_commission_usd + exit_commission_usd
        
        # Расчет комиссий в BNB (если применима скидка)
        if has_bnb_discount:
            entry_commission_bnb = entry_commission_usd / bnb_price_usd
            exit_commission_bnb = exit_commission_usd / bnb_price_usd
            total_commission_bnb = total_commission_usd / bnb_price_usd
        else:
            entry_commission_bnb = 0
            exit_commission_bnb = 0
            total_commission_bnb = 0
        
        return CommissionCalculation(
            entry_commission_usd=entry_commission_usd,
            exit_commission_usd=exit_commission_usd,
            total_commission_usd=total_commission_usd,
            entry_commission_bnb=entry_commission_bnb,
            exit_commission_bnb=exit_commission_bnb,
            total_commission_bnb=total_commission_bnb,
            has_bnb_discount=has_bnb_discount,
            commission_rate_used=commission_rate
        )
    
    def calculate_bnb_requirement(
        self,
        trade_amount_usd: float,
        current_bnb_balance: float,
        bnb_price_usd: float
    ) -> BNBRequirement:
        """
        Рассчитывает требования к BNB для сделки
        """
        
        # Рассчитываем необходимое количество BNB для комиссий
        commission_calc = self.calculate_commissions(trade_amount_usd, current_bnb_balance, bnb_price_usd)
        required_bnb = commission_calc.total_commission_bnb
        
        # Добавляем коэффициент безопасности
        required_bnb_with_safety = required_bnb * self.bnb_safety_multiplier
        
        # Проверяем достаточность
        is_sufficient = current_bnb_balance >= required_bnb_with_safety
        
        return BNBRequirement(
            required_bnb=required_bnb,
            required_bnb_with_safety=required_bnb_with_safety,
            current_bnb_balance=current_bnb_balance,
            is_sufficient=is_sufficient,
            bnb_price_usd=bnb_price_usd,
            safety_multiplier=self.bnb_safety_multiplier
        )
    
    def calculate_stop_loss(
        self,
        entry_price: float,
        target_profit_usd: float,
        trade_amount_usd: float,
        bnb_balance: float = 0,
        bnb_price_usd: float = 300.0
    ) -> StopLossCalculation:
        """
        Рассчитывает стоп-лосс по нашей логике:
        Стоп-лосс = max(комиссии, 50% от целевого профита)
        """
        
        # Рассчитываем комиссии
        commission_calc = self.calculate_commissions(trade_amount_usd, bnb_balance, bnb_price_usd)
        commission_amount = commission_calc.total_commission_usd
        
        # 50% от целевого профита
        profit_ratio_amount = target_profit_usd * self.stop_loss_profit_ratio
        
        # Выбираем максимальное значение
        stop_loss_amount_usd = max(commission_amount, profit_ratio_amount)
        
        # Определяем причину
        if stop_loss_amount_usd == commission_amount:
            reason = "commission_based"
        else:
            reason = "profit_ratio_based"
        
        # Рассчитываем цену стоп-лосса
        # Для BUY позиции: entry_price - stop_loss_amount_per_unit
        btc_quantity = trade_amount_usd / entry_price
        stop_loss_per_unit = stop_loss_amount_usd / btc_quantity
        stop_loss_price = entry_price - stop_loss_per_unit
        
        return StopLossCalculation(
            stop_loss_price=stop_loss_price,
            stop_loss_amount_usd=stop_loss_amount_usd,
            reason=reason,
            commission_amount=commission_amount,
            profit_ratio_amount=profit_ratio_amount
        )
    
    def calculate_trailing_thresholds(
        self,
        target_profit_usd: float,
        trailing_percent: float = 20.0
    ) -> Dict[str, float]:
        """
        Рассчитывает пороги для трейлинг стопа
        """
        
        trailing_amount = target_profit_usd * (trailing_percent / 100)
        
        return {
            "target_profit_usd": target_profit_usd,
            "trailing_activation_usd": target_profit_usd,  # Активируется при достижении целевого профита
            "trailing_amount_usd": trailing_amount,  # Отступ от максимального профита
            "trailing_percent": trailing_percent,
            "minimum_exit_profit_usd": target_profit_usd - trailing_amount  # Не продавать ниже этого
        }
    
    def check_trade_viability(
        self,
        trade_amount_usd: float,
        target_profit_usd: float,
        entry_price: float,
        current_bnb_balance: float,
        bnb_price_usd: float,
        min_trade_amount: float = 10.0
    ) -> Tuple[bool, str, Dict]:
        """
        Проверяет жизнеспособность сделки перед её созданием
        
        Returns:
            (is_viable, reason, details)
        """
        
        details = {}
        
        # Проверка минимальной суммы сделки
        if trade_amount_usd < min_trade_amount:
            return False, f"Trade amount too small: {trade_amount_usd} < {min_trade_amount}", details
        
        # Расчет комиссий
        commission_calc = self.calculate_commissions(trade_amount_usd, current_bnb_balance, bnb_price_usd)
        details["commissions"] = commission_calc
        
        # Проверка BNB требований
        bnb_req = self.calculate_bnb_requirement(trade_amount_usd, current_bnb_balance, bnb_price_usd)
        details["bnb_requirement"] = bnb_req
        
        if not bnb_req.is_sufficient:
            return False, f"Insufficient BNB: need {bnb_req.required_bnb_with_safety:.6f}, have {current_bnb_balance:.6f}", details
        
        # Расчет стоп-лосса
        stop_loss_calc = self.calculate_stop_loss(entry_price, target_profit_usd, trade_amount_usd, current_bnb_balance, bnb_price_usd)
        details["stop_loss"] = stop_loss_calc
        
        # Проверка адекватности стоп-лосса (не должен быть слишком близко к цене входа)
        stop_loss_percent = ((entry_price - stop_loss_calc.stop_loss_price) / entry_price) * 100
        if stop_loss_percent > 10:  # Если стоп-лосс больше 10% от цены входа
            return False, f"Stop loss too large: {stop_loss_percent:.2f}% of entry price", details
        
        # Расчет трейлинга
        trailing = self.calculate_trailing_thresholds(target_profit_usd)
        details["trailing"] = trailing
        
        # Проверка соотношения риск/прибыль
        risk_reward_ratio = target_profit_usd / stop_loss_calc.stop_loss_amount_usd
        details["risk_reward_ratio"] = risk_reward_ratio
        
        if risk_reward_ratio < 1.5:  # Минимальное соотношение 1:1.5
            return False, f"Poor risk/reward ratio: {risk_reward_ratio:.2f} (minimum 1.5)", details
        
        # Все проверки пройдены
        return True, "Trade viable", details
    
    def calculate_position_size(
        self,
        available_balance: float,
        entry_price: float,
        target_profit_usd: float,
        risk_percent_of_balance: float = 2.0,
        current_bnb_balance: float = 0,
        bnb_price_usd: float = 300.0
    ) -> Dict[str, Any]:
        """
        Рассчитывает оптимальный размер позиции на основе баланса и риска
        """
        
        # Максимальная сумма риска (% от баланса)
        max_risk_amount = available_balance * (risk_percent_of_balance / 100)
        
        # Расчет стоп-лосса для определения максимальной суммы сделки
        # Итеративный подход: пробуем разные размеры позиций
        best_trade_amount = 0
        best_details = None
        
        for risk_multiplier in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
            test_trade_amount = max_risk_amount * risk_multiplier
            
            if test_trade_amount > available_balance * 0.1:  # Не более 10% от баланса
                continue
            
            # Проверяем жизнеспособность
            is_viable, reason, details = self.check_trade_viability(
                test_trade_amount,
                target_profit_usd,
                entry_price,
                current_bnb_balance,
                bnb_price_usd
            )
            
            if is_viable and details["stop_loss"].stop_loss_amount_usd <= max_risk_amount:
                best_trade_amount = test_trade_amount
                best_details = details
                break
        
        if best_trade_amount == 0:
            return {
                "success": False,
                "reason": "Cannot find suitable position size",
                "recommended_action": "Increase balance or reduce target profit"
            }
        
        # Рассчитываем количество BTC
        btc_quantity = best_trade_amount / entry_price
        
        return {
            "success": True,
            "trade_amount_usd": best_trade_amount,
            "btc_quantity": btc_quantity,
            "risk_amount_usd": best_details["stop_loss"].stop_loss_amount_usd,
            "risk_percent_of_balance": (best_details["stop_loss"].stop_loss_amount_usd / available_balance) * 100,
            "details": best_details
        }
    
    def format_risk_report(self, trade_amount_usd: float, details: Dict) -> str:
        """Формирует отчет о рисках для логирования"""
        
        commission = details["commissions"]
        stop_loss = details["stop_loss"]
        bnb_req = details["bnb_requirement"]
        trailing = details["trailing"]
        
        report = f"""
RISK ANALYSIS REPORT
====================
Trade Amount: ${trade_amount_usd:.2f}
Entry Price: ${details.get('entry_price', 'N/A')}

COMMISSIONS:
- Total: ${commission.total_commission_usd:.2f} ({commission.commission_rate_used*100:.3f}%)
- BNB Discount: {'Yes' if commission.has_bnb_discount else 'No'}
- BNB Required: {commission.total_commission_bnb:.6f}

STOP LOSS:
- Price: ${stop_loss.stop_loss_price:.2f}
- Amount: ${stop_loss.stop_loss_amount_usd:.2f}
- Reason: {stop_loss.reason}

BNB REQUIREMENT:
- Required (with safety): {bnb_req.required_bnb_with_safety:.6f}
- Current Balance: {bnb_req.current_bnb_balance:.6f}
- Sufficient: {'Yes' if bnb_req.is_sufficient else 'No'}

TRAILING SETUP:
- Activation at: ${trailing['trailing_activation_usd']:.2f} profit
- Trailing amount: ${trailing['trailing_amount_usd']:.2f} ({trailing['trailing_percent']}%)
- Minimum exit: ${trailing['minimum_exit_profit_usd']:.2f} profit

RISK/REWARD:
- Risk: ${stop_loss.stop_loss_amount_usd:.2f}
- Reward: ${trailing['target_profit_usd']:.2f}
- Ratio: 1:{details.get('risk_reward_ratio', 'N/A'):.2f}
====================
"""
        
        return report


# Глобальный экземпляр калькулятора
risk_calculator = RiskCalculator()