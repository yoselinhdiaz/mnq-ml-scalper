"""
tools/test_execution.py
Prueba completa de ejecución: conecta a MT5, inspecciona símbolo,
envía una orden BUY mínima, verifica que quedó abierta, y la cierra.
"""
import sys
import time
import yaml
import MetaTrader5 as mt5

FILLING_NAMES = {
    mt5.ORDER_FILLING_FOK:    "FOK",
    mt5.ORDER_FILLING_IOC:    "IOC",
    mt5.ORDER_FILLING_RETURN: "RETURN",
}

def connect(cfg):
    ok = mt5.initialize(
        login=cfg["mt5"]["login"],
        password=cfg["mt5"]["password"],
        server=cfg["mt5"]["server"],
    )
    if not ok:
        print(f"[ERROR] MT5 init failed: {mt5.last_error()}")
        sys.exit(1)
    info = mt5.account_info()
    print(f"[OK] Conectado — cuenta {info.login} | balance ${info.balance:,.2f}")
    return info

def inspect_symbol(symbol):
    mt5.symbol_select(symbol, True)
    si = mt5.symbol_info(symbol)
    if si is None:
        print(f"[ERROR] symbol_info failed: {mt5.last_error()}")
        sys.exit(1)

    # filling_mode bitmask: bit0=FOK(1), bit1=IOC(2); RETURN if neither set
    filling_raw = si.filling_mode
    supported_orders = []
    if filling_raw & 1: supported_orders.append(("FOK",    mt5.ORDER_FILLING_FOK))
    if filling_raw & 2: supported_orders.append(("IOC",    mt5.ORDER_FILLING_IOC))
    if not supported_orders:
        supported_orders.append(("RETURN", mt5.ORDER_FILLING_RETURN))

    tick = mt5.symbol_info_tick(symbol)
    stop_level = si.trade_stops_level  # min stop distance in points
    print(f"\n[SYMBOL] {symbol}")
    print(f"  point         = {si.point}")
    print(f"  tick_value    = {si.trade_tick_value}")
    print(f"  min_lot       = {si.volume_min}")
    print(f"  stop_level    = {stop_level} points ({stop_level * si.point:.2f} price units)")
    print(f"  filling_mode  = {filling_raw} -> soporta: {[n for n,_ in supported_orders]}")
    print(f"  bid/ask       = {tick.bid:.1f} / {tick.ask:.1f}")
    return si, tick, supported_orders

def try_order(symbol, magic, si, tick, filling_name, filling_code):
    lots  = si.volume_min
    price = tick.ask
    point = si.point

    # Stops seguros: minimo 2x stop_level, usamos 800 puntos (~8 price units para US100)
    min_dist = max((si.trade_stops_level + 10) * point, 8.0)
    sl = round(price - min_dist * 1.2, 2)
    tp = round(price + min_dist * 2.0, 2)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lots,
        "type":         mt5.ORDER_TYPE_BUY,
        "price":        round(price, 2),
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        magic,
        "comment":      "test_execution",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_code,
    }

    print(f"\n[TEST] Enviando BUY {lots} lotes @ {price:.1f} | SL={sl:.2f} TP={tp:.2f} | filling={filling_name}")
    result = mt5.order_send(request)

    if result is None:
        print(f"[FAIL] order_send devolvió None — {mt5.last_error()}")
        return None

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[FAIL] retcode={result.retcode} | {result.comment}")
        return None

    print(f"[OK]   Orden abierta — ticket={result.order} | deal={result.deal}")
    return result.order

def close_position(ticket, symbol, magic):
    time.sleep(1)
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        print(f"[WARN] Posición {ticket} no encontrada (puede haber cerrado por SL/TP)")
        return

    pos  = positions[0]
    tick = mt5.symbol_info_tick(symbol)
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       pos.volume,
        "type":         mt5.ORDER_TYPE_SELL,
        "position":     ticket,
        "price":        round(tick.bid, 2),
        "deviation":    20,
        "magic":        magic,
        "comment":      "test_close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[OK]   Posición cerrada — PnL en cuenta actualizado")
    else:
        code = result.retcode if result else "None"
        print(f"[FAIL] Cierre fallido — retcode={code}")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    symbol = cfg["mt5"]["symbol"]
    magic  = cfg["mt5"]["magic"]

    connect(cfg)
    si, tick, supported_orders = inspect_symbol(symbol)

    if not supported_orders:
        print("[WARN] Broker no reporta filling modes — probando IOC por defecto")
        supported_orders = [("IOC", mt5.ORDER_FILLING_IOC)]

    ticket = None
    for name, code in supported_orders:
        ticket = try_order(symbol, magic, si, tick, name, code)
        if ticket:
            close_position(ticket, symbol, magic)
            print(f"\n[RESULTADO] Filling '{name}' funciona correctamente.")
            if name != "IOC":
                print(f"  -> Cambia ORDER_FILLING_IOC -> ORDER_FILLING_{name} en order_sender.py")
            else:
                print(f"  -> order_sender.py ya usa IOC, todo correcto.")
            break
        print(f"[RETRY] {name} fallido, probando siguiente...")

    if not ticket:
        print("\n[RESULTADO] Ningún filling mode funciono — revisa MT5 Expert Advisors habilitados.")

    mt5.shutdown()
