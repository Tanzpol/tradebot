import os
from typing import Dict, List
from binance.spot import Spot as BinanceClient
from config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET

def make_client() -> BinanceClient:
    if BINANCE_TESTNET == "1":
        return BinanceClient(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET,
                             base_url="https://testnet.binance.vision")
    return BinanceClient(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

def fetch_balances() -> List[Dict]:
    """
    Возвращает список активов с ненулевым total:
    [{asset, free, locked, total}], отсортированный по total (убывание).
    """
    client = make_client()
    acct = client.account()
    out: List[Dict] = []
    for a in acct.get("balances", []):
        free = float(a["free"]); locked = float(a["locked"])
        total = free + locked
        if total > 0:
            out.append({
                "asset": a["asset"],
                "free": free,
                "locked": locked,
                "total": total,
            })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out
