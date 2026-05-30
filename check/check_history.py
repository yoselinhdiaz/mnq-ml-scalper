# check_history.py
import MetaTrader5 as mt5

mt5.initialize()
rates = mt5.copy_rates_from_pos("US100", mt5.TIMEFRAME_M1, 0, 10000)
print(f"Barras disponibles: {len(rates) if rates is not None else 0}")
print(f"Desde: {rates[0]['time'] if rates is not None else 'N/A'}")
print(f"Hasta: {rates[-1]['time'] if rates is not None else 'N/A'}")
mt5.shutdown()
