"""Test de interrupcion: 3 TTS consecutivas, la 2da y 3ra deben interrumpir."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import tts_client, cli

async def main():
    #  Tira 3 narraciones seguidas. La primera deberia cortarse al llegar
    #  la segunda, y la segunda al llegar la tercera.
    print(">> 1ra: invalida")
    cli._narrate_invalid("remover_inventario", {"producto": "", "cantidad": 108, "unidad": "Munda"})
    await asyncio.sleep(0.5)

    print(">> 2da: no_action")
    cli._narrate_no_action()
    await asyncio.sleep(0.5)

    print(">> 3ra: aceptada")
    cli._narrate_aceptada(
        "agregar_inventario",
        {"producto": "papa", "cantidad": 4, "unidad": "Kilogram"},
        {"nombre": "papa", "stock_actual": 54, "unidad": "Kilogram", "bodega": "almacen general"},
    )

    #  Dejar terminar la ultima
    await asyncio.sleep(6)
    print(">> fin")

asyncio.run(main())
