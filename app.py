import time
from datetime import datetime
import MetaTrader5 as mt5

# --- CONFIGURATION ---
SYMBOL = "XAUUSD"
LOT_SIZE = 0.1
MAGIC_NUMBER = 100200
TIMEFRAME_MINUTES = 15

def initialize_mt5():
    if not mt5.initialize():
        print("❌ MetaTrader 5 initialize fail ho gaya. Error:", mt5.last_error())
        return False
    print("✅ MetaTrader 5 Successfully Connected!")
    return True

def get_live_rates(symbol):
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"❌ Symbol {symbol} nahi mila.")
        return None
    if not symbol_info.select:
        mt5.symbol_select(symbol, True)
    return mt5.symbol_info_tick(symbol)

def open_trade(symbol, action_type, lot, sl_points=4.0, tp_points=8.0):
    tick = get_live_rates(symbol)
    if not tick:
        return
    
    price = tick.ask if action_type == "BUY" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if action_type == "BUY" else mt5.ORDER_TYPE_SELL
    
    sl = price - sl_points if action_type == "BUY" else price + sl_points
    tp = price + tp_points if action_type == "BUY" else price - tp_points
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "Price Lesson Auto-Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"❌ Trade execution fail hua: {result.comment}")
    else:
        print(f"🚀 Live Trade Placed Successfully! Ticket #{result.order} | Price: {price} | SL: {sl} | TP: {tp}")

if __name__ == "__main__":
    if initialize_mt5():
        tick = get_live_rates(SYMBOL)
        if tick:
            print(f"🪙 Live Gold Rate (Ask/Bid): {tick.ask} / {tick.bid}")
            # Demo trade test execution:
            # open_trade(SYMBOL, "BUY", LOT_SIZE)
        mt5.shutdown()
