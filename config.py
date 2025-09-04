
import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "1")  # "1"=testnet, "0"=mainnet

# Flask
FLASK_SECRET = os.getenv("FLASK_SECRET", "dev-secret")
TOP_ASSETS = [a.strip() for a in os.getenv("TOP_ASSETS", "BTC,USDC,BNB,ETH,TON").split(",") if a.strip()]

# Комиссии и запас BNB
FEE_RATE_BNB = float(os.getenv("FEE_RATE_BNB", "0.00075"))
FEE_RATE_NO_BNB = float(os.getenv("FEE_RATE_NO_BNB", "0.001"))
BNB_SAFETY = float(os.getenv("BNB_SAFETY", "1.2"))
