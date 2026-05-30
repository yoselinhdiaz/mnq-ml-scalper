# check_symbol.py
import MetaTrader5 as mt5

mt5.initialize()

for name in ["US100", "NAS100", "USTEC", "US100.cash", "NAS100.cash"]:
    info = mt5.symbol_info(name)
    if info:
        print(f"✓ ENCONTRADO: {name}")
        print(f"  Punto:       {info.point}")
        print(f"  Tick value:  {info.trade_tick_value}")
        print(f"  Lote min:    {info.volume_min}")
        print(f"  Spread:      {info.spread}")
        break
    else:
        print(f"✗ {name} — no encontrado")

mt5.shutdown()