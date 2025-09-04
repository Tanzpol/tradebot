# bot.py
import os
import json
import threading
import time
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP, getcontext

from binance.client import Client
from config import BINANCE_API_KEY, BINANCE_API_SECRET

# Точность Decimal
getcontext().prec = 28

# Флаг тестовой сети и комиссия
TESTNET = os.getenv("BINANCE_TESTNET") == "1"
# В testnet комиссии фактически нет; в реале берём 0.075% (если платишь BNB)
FEE_RATE_D = Decimal("0.00000") if TESTNET else Decimal("0.00075")


class TradeBot:
    def __init__(self):
        self._ws_started = {}
        self._ws_threads = {}
        self._ws_stops = {}
        self.testnet = TESTNET
        self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=self.testnet)

        self.current_prices: Dict[str, float] = {}  # кэш цен по парам

        # WebSocket поток цены BTCUSDT
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_stop = threading.Event()
        self._ws_started[symbol] = False

        # Мониторинг открытой сделки
        self._mon_thread: Optional[threading.Thread] = None
        self._mon_stop = threading.Event()

        self.active_trade = None  # параметры текущей сделки

    # ---- Вспомогательные форматтеры ----
    @staticmethod
    def _q2(q: Decimal) -> str:
        return str(q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _q8(q: Decimal) -> str:
        return str(q.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))

    # ---- WebSocket BTCUSDT ----
    def _ws_loop(self, symbol: str):
        import websocket  # импорт внутри потока, чтобы не конфликтовать с uvicorn
        url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"

        def on_message(ws, message):
            try:
                msg = json.loads(message)
                p = float(msg.get("p"))
                self.current_price = p
            except Exception:
                pass

        def on_error(ws, err):
            print("WS error:", err)

        def on_close(ws, code, reason):
            print("WS closed:", code, reason)

        while not self._ws_stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print("WS exception:", e)
            if not self._ws_stop.is_set():
                time.sleep(2)

    def start_price_stream(self, symbol: str):
        if self._ws_started.get(symbol):
            return
        self._ws_stops[symbol] = threading.Event()
        self._ws_threads[symbol] = threading.Thread(target=self._ws_loop, args=(symbol,), daemon=True)
        self._ws_threads[symbol].start()
        self._ws_started[symbol] = True

    def stop_price_stream(self, symbol: str):
        if not self._ws_started.get(symbol):
            return
        self._ws_stops[symbol].set()
        self._ws_started[symbol] = False

    # ---- Расчёты PnL ----
    def _pnl_usdc_now(self, qty_btc_str: str, spent_usdc_str: str, price: Optional[float]) -> Optional[Decimal]:
        """Текущий PnL в USDC с учётом комиссии продажи."""
        if price is None:
            return None
        qty = Decimal(qty_btc_str)
        spent = Decimal(spent_usdc_str)
        gross = qty * Decimal(str(price))
        fee = (gross * FEE_RATE_D)
        return gross - fee - spent

    # ---- Торговля ----
    def buy(self, amount_usdc: float, take_profit: float, stop_loss: float, trailing: float):
        """
        Покупка BTCUSDT MARKET-ордером на сумму в USDC (quoteOrderQty).
        Цена входа фиксируется через Decimal и хранится строкой.
        """
        quote_qty = Decimal(str(amount_usdc)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if quote_qty <= 0:
            raise ValueError("amount_usdc must be > 0")

        order = self.client.create_order(
            symbol="BTCUSDT",
            side="BUY",
            type="MARKET",
            quoteOrderQty=str(quote_qty),
        )

        executed_qty = Decimal(order.get("executedQty", "0"))
        cumm_quote = Decimal(order.get("cummulativeQuoteQty", "0"))
        avg_price = (cumm_quote / executed_qty) if executed_qty > 0 else None

        # Фиксируем значения в виде строк с нужной точностью — они больше не меняются
        entry_price_str = self._q2(avg_price) if avg_price is not None else None
        spent_usdc_str = self._q2(cumm_quote)
        qty_btc_str = self._q8(executed_qty)

        self.active_trade = {
            # то, что просили купить (для наглядности)
            "requested_usdc": float(quote_qty),

            # фактическое исполнение
            "amount_usdc": float(quote_qty),          # дублируем для совместимости со старым UI
            "qty_btc": qty_btc_str,                   # строка с 8 знаками
            "entry_price": entry_price_str,           # строка с 2 знаками (заморожена)
            "spent_usdc": spent_usdc_str,             # строка с 2 знаками
            "order_id": order.get("orderId"),

            # параметры выхода
            "take_profit_usdc": float(take_profit),
            "stop_loss_usdc": float(stop_loss),
            "trailing_percent": float(trailing),

            # прочее
            "status": "open",
            "testnet": self.testnet,

            # runtime:
            "high_watermark": float(avg_price) if avg_price is not None else None,
            "last_reason": None,
        }

        # запуск мониторинга
        self._start_monitor()
        return self.active_trade

    def _start_monitor(self):
        self._stop_monitor()
        self._mon_stop.clear()
        self._mon_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._mon_thread.start()

    def _stop_monitor(self):
        try:
            if self._mon_thread and self._mon_thread.is_alive():
                self._mon_stop.set()
                self._mon_thread.join(timeout=1.0)
        except Exception:
            pass
        finally:
            self._mon_thread = None
            self._mon_stop.clear()

    def _monitor_loop(self):
        """Постоянная проверка условий выхода: TP$, SL$, Trailing%."""
        while not self._mon_stop.is_set():
            try:
                trade = self.active_trade
                price = self.current_prices.get(trade["symbol"])
                if not trade or trade.get("status") != "open" or price is None:
                    time.sleep(0.2)
    def _background_sync(self):
        """Фоновая синхронизация с файлом раз в 10 секунд"""
        while not self._mon_stop.is_set():
            try:
                for trade in self.active_trades.values():
                    self._sync_trade_to_state(trade)
            except Exception as e:
                print(f"Ошибка синхронизации: {e}")
            time.sleep(10)
    
    def _start_monitor(self):
        self._stop_monitor()
        self._mon_stop.clear()
        self._mon_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._mon_thread.start()
        # Запускаем фоновую синхронизацию
        sync_thread = threading.Thread(target=self._background_sync, daemon=True)
        sync_thread.start()
                    continue

                qty_str = trade["qty_btc"]
                spent_str = trade["spent_usdc"]
                tp_usd = trade["take_profit_usdc"]
                sl_usd = trade["stop_loss_usdc"]
                trailing_pct = trade["trailing_percent"]

                # Обновляем максимум цены после входа
                if trade["high_watermark"] is None or price > trade["high_watermark"]:
                    trade["high_watermark"] = price

                pnl = self._pnl_usdc_now(qty_str, spent_str, price)
                if pnl is None:
                    time.sleep(0.2)
                    continue

                # 1) Stop-Loss $ (жёсткая защита капитала)
                if sl_usd > 0 and pnl <= Decimal(str(-abs(sl_usd))):
                    trade["last_reason"] = f"STOP LOSS ${sl_usd} (PnL={pnl.quantize(Decimal('0.01'))})"
                    self._sell_all()
                    continue

                # 2) Take Profit $ (жёсткий порог прибыли)
                if tp_usd > 0 and pnl >= Decimal(str(abs(tp_usd))):
                    trade["last_reason"] = f"TAKE PROFIT ${tp_usd} (PnL={pnl.quantize(Decimal('0.01'))})"
                    self._sell_all()
                    continue

                # 3) Trailing Take Profit % (продаём при откате от максимумов, если уже в плюсе)
                hw = trade.get("high_watermark")
                if hw and trailing_pct > 0:
                    drop_trigger = hw * (1 - float(trailing_pct) / 100.0)
                    if price < drop_trigger and pnl > Decimal("0"):
                        trade["last_reason"] = (
                            f"TRAILING {trailing_pct}% (from {hw:.2f} "
                            f"to {price:.2f}, PnL={pnl.quantize(Decimal('0.01'))})"
                        )
                        self._sell_all()
                        continue

            except Exception as e:
                print("Monitor error:", e)

            time.sleep(0.2)

    def _sell_all(self):
        """MARKET-продажа всего объёма BTCUSDT по рынку, закрытие сделки."""
        trade = self.active_trade
        if not trade or trade.get("status") != "open":
            return

        qty = Decimal(trade["qty_btc"])
        # адекватное строковое представление количества
        qty_str = str(qty.normalize())
        try:
            order = self.client.create_order(
                symbol="BTCUSDT",
                side="SELL",
                type="MARKET",
                quantity=qty_str,
            )
            trade["close_order_id"] = order.get("orderId")
            cumm_quote = Decimal(order.get("cummulativeQuoteQty", "0"))
            fee = (cumm_quote * FEE_RATE_D)
            pnl = cumm_quote - fee - Decimal(trade["spent_usdc"])
            trade["realized_pnl_usdc"] = str(pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            trade["status"] = "closed"
        except Exception as e:
            trade["last_reason"] = f"SELL ERROR: {e}"

    
    def _sync_trade_to_state(self, trade: dict):
        """Синхронизирует сделку с trade_state.json."""
        from state import load_state, save_state
        try:
            state = load_state()
            for i, t in enumerate(state["trades"]):
                if t["id"] == trade["id"]:
                    state["trades"][i] = trade.copy()
                    save_state(state)
                    break
        except Exception as e:
            print(f"Ошибка синхронизации сделки {trade.get('id')}: {e}")
    def status(self):
        """
        Возвращаем состояние для UI.
        Добавляем:
          - current_value_usdc: текущая стоимость позиции с комиссией
          - diff_vs_spent_usdc: разница текущей стоимости и реально потраченного
        """
        base = {
            "status": self.active_trade["status"] if self.active_trade else "idle",
            "current_price": round(self.current_price, 2) if self.current_price else None,
            "testnet": self.testnet
        }
        if self.active_trade:
            t = dict(self.active_trade)  # копия

            # Текущая оценка позиции и дельта относительно spend
            try:
                price = self.current_prices.get(trade["symbol"])
                if price is not None and t.get("qty_btc") and t.get("spent_usdc"):
                    qty = Decimal(t["qty_btc"])
                    spent = Decimal(t["spent_usdc"])
                    gross = qty * Decimal(str(price))
                    fee = (gross * FEE_RATE_D)
                    current_value = gross - fee
                    t["current_value_usdc"] = str(current_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    t["diff_vs_spent_usdc"] = str((current_value - spent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except Exception:
                pass

            base["trade"] = t
        return base
