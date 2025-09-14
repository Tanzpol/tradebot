#!/usr/bin/env python3
"""
Стартовый скрипт для Trading Bot v2
Проверяет зависимости, конфигурацию и запускает бота
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

def check_python_version():
    """Проверяет версию Python"""
    if sys.version_info < (3, 8):
        print("❌ Error: Python 3.8+ required")
        print(f"Current version: {sys.version}")
        sys.exit(1)
    print(f"✅ Python version: {sys.version.split()[0]}")

def check_dependencies():
    """Проверяет установленные зависимости"""
    required_packages = [
        'fastapi', 'uvicorn', 'websockets', 'binance', 
        'dotenv', 'pydantic', 'aiofiles', 'requests',
        'pandas', 'numpy'
    ]
    
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package.replace('-', '_'))
            print(f"✅ {package}")
        except ImportError:
            missing_packages.append(package)
            print(f"❌ {package} - missing")
    
    if missing_packages:
        print(f"\n❌ Missing packages: {', '.join(missing_packages)}")
        print("Install with: pip install -r requirements.txt")
        sys.exit(1)

def check_environment():
    """Проверяет переменные окружения"""
    from dotenv import load_dotenv
    load_dotenv()
    
    required_vars = ['BINANCE_API_KEY', 'BINANCE_API_SECRET']
    optional_vars = ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    
    missing_required = []
    
    for var in required_vars:
        value = os.getenv(var)
        if value:
            print(f"✅ {var}: {'*' * (len(value) - 8)}{value[-8:]}")
        else:
            missing_required.append(var)
            print(f"❌ {var}: not set")
    
    for var in optional_vars:
        value = os.getenv(var)
        if value:
            print(f"✅ {var}: {'*' * (len(value) - 4)}{value[-4:]}")
        else:
            print(f"⚠️  {var}: not set (optional)")
    
    if missing_required:
        print(f"\n❌ Required environment variables missing: {', '.join(missing_required)}")
        print("Please set them in .env file")
        sys.exit(1)

def create_directories():
    """Создает необходимые директории"""
    dirs = ['data', 'data/state', 'data/trades', 'logs']
    
    for dir_path in dirs:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
        print(f"✅ Directory: {dir_path}")

def check_binance_connection():
    """Проверяет подключение к Binance API"""
    print("\n🔍 Testing Binance API connection...")
    
    sys.path.append('backend')
    
    try:
        from binance_client import BinanceRESTClient
        from dotenv import load_dotenv
        
        load_dotenv()
        
        api_key = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_API_SECRET")
        testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
        
        client = BinanceRESTClient(api_key, api_secret, testnet)
        
        async def test_connection():
            connected = await client.test_connection()
            if connected:
                print("✅ Binance API connection successful")
                
                # Проверяем получение баланса
                account_info = await client.get_account_info()
                if account_info:
                    print("✅ Account info retrieved")
                    
                    # Показываем основные балансы
                    balances = account_info.get("balances", [])
                    main_assets = ["USDT", "USDC", "BTC", "BNB"]
                    
                    print("\n💰 Main balances:")
                    for balance in balances:
                        if balance["asset"] in main_assets:
                            free = float(balance["free"])
                            locked = float(balance["locked"])
                            if free > 0 or locked > 0:
                                print(f"   {balance['asset']}: {free:.8f} (free) + {locked:.8f} (locked)")
                else:
                    print("⚠️  Could not retrieve account info")
                
                await client.close()
                return True
            else:
                print("❌ Binance API connection failed")
                await client.close()
                return False
        
        return asyncio.run(test_connection())
        
    except Exception as e:
        print(f"❌ Error testing Binance connection: {e}")
        return False

def check_telegram():
    """Проверяет Telegram уведомления"""
    print("\n📱 Testing Telegram notifications...")
    
    sys.path.append('backend')
    
    try:
        from telegram_notifier import TelegramNotifier
        
        notifier = TelegramNotifier()
        
        if not notifier.enabled:
            print("⚠️  Telegram notifications disabled (missing credentials)")
            return True
        
        async def test_telegram():
            success = await notifier.test_connection()
            await notifier.close()
            
            if success:
                print("✅ Telegram notifications working")
            else:
                print("❌ Telegram test failed")
            
            return success
        
        return asyncio.run(test_telegram())
        
    except Exception as e:
        print(f"❌ Error testing Telegram: {e}")
        return False

def show_startup_info():
    """Показывает информацию о запуске"""
    from dotenv import load_dotenv
    load_dotenv()
    
    testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
    port = os.getenv("PORT", "8080")
    target_profit = os.getenv("TARGET_PROFIT_USD", "50.0")
    max_trades = os.getenv("MAX_CONCURRENT_TRADES", "10")
    
    print(f"""
🚀 Trading Bot v2 Configuration:
   Mode: {'🧪 Testnet' if testnet else '💰 Production'}
   Port: {port}
   Target Profit: ${target_profit}
   Max Trades: {max_trades}
   
🌐 Web Interface: http://localhost:{port}
📊 API Health: http://localhost:{port}/api/health
📈 Trades: http://localhost:{port}/api/trades

Press Ctrl+C to stop the bot
""")

def main():
    """Главная функция запуска"""
    print("🤖 Trading Bot v2 - Startup Check")
    print("=" * 50)
    
    # Проверки
    print("\n1️⃣ Checking Python version...")
    check_python_version()
    
    print("\n2️⃣ Checking dependencies...")
    check_dependencies()
    
    print("\n3️⃣ Checking environment variables...")
    check_environment()
    
    print("\n4️⃣ Creating directories...")
    create_directories()
    
    print("\n5️⃣ Testing Binance API...")
    if not check_binance_connection():
        print("❌ Binance API test failed. Please check your credentials.")
        sys.exit(1)
    
    print("\n6️⃣ Testing Telegram...")
    check_telegram()
    
    print("\n✅ All checks passed!")
    
    show_startup_info()
    
    # Запуск приложения
    try:
        os.chdir("backend")
        os.system("python main.py")
    except KeyboardInterrupt:
        print("\n\n👋 Trading Bot v2 stopped by user")
    except Exception as e:
        print(f"\n❌ Error running bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
