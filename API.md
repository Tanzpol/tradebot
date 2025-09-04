# TradeBot API

Все эндпойнты слушают на `http://<server>:5000`.

## Здоровье

GET /
→ {"status":"ok","ts":...}

## Балансы

GET /api/balances
→ {"balances":{"USDT":1000,"BNB":2},"ts":...}

## Сделки

### Список

GET /api/trades
→ {"trades":[...],"ts":...}

### Создать

POST /api/trades
Content-Type: application/json

{
"symbol": "BTCUSDC",
"amount_usdc": 100,
"entry_price": 110000,
"qty": 0.001,
"spent_usdc": 100
}
→ {"id":1,...}

### Удалить одну

DELETE /api/trades/<id>
→ 204 No Content

### Удалить все

DELETE /api/trades
→ 204 No Content

## Сетка

POST /api/trades/grid
{...}
→ создаёт несколько сделок (пример логики см. main.py)

## Проверка комиссий

POST /api/fee_check
{...}
→ {"enough":true,...}

## Сброс

POST /api/reset
→ сбрасывает состояние
