from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db_client as db

app = FastAPI(
    title="Cactus Inventory Tools",
    description="Tool server para el demo de inventario con Filtro de Kalman + Cactus",
    version="1.0.0",
)


class AgregarRequest(BaseModel):
    producto: str
    cantidad: int


class RemoverRequest(BaseModel):
    producto: str
    cantidad: int


class ConsultarRequest(BaseModel):
    producto: str | None = None


class InvestigarRequest(BaseModel):
    producto: str | None = None


class ConfirmarRequest(BaseModel):
    movimiento_id: int
    confirmar: bool


class ToolResponse(BaseModel):
    success: bool
    message: str
    data: dict | list | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tool/agregar_inventario", response_model=ToolResponse)
def agregar_inventario(req: AgregarRequest):
    if req.cantidad <= 0:
        raise HTTPException(400, "La cantidad debe ser positiva")

    prod = db.query_one(
        "SELECT id, nombre, stock_actual FROM productos WHERE nombre = %s",
        (req.producto,),
    )
    if not prod:
        return ToolResponse(
            success=False,
            message=f"Producto '{req.producto}' no encontrado. Productos disponibles: papa, cebolla, tomate, zanahoria, ajo",
        )

    db.execute(
        "INSERT INTO inventario_movimientos (producto_id, tipo, cantidad_reportada) VALUES (%s, 'entrada', %s)",
        (prod["id"], req.cantidad),
    )

    mov = db.query_one(
        "SELECT id, decision_kalman, residual_kalman, umbral_usado, stock_resultante FROM inventario_movimientos WHERE producto_id = %s ORDER BY id DESC LIMIT 1",
        (prod["id"],),
    )

    decision = mov["decision_kalman"]
    residual = mov["residual_kalman"]
    umbral = mov["umbral_usado"]
    nuevo_stock = mov["stock_resultante"]

    if decision == "ACEPTADA":
        return ToolResponse(
            success=True,
            message=f"Movimiento ACEPTADO por Kalman. {req.producto}: {prod['stock_actual']} -> {nuevo_stock} | residual: {residual:.1f}s | umbral: {umbral:.1f}",
            data={
                "movimiento_id": mov["id"],
                "decision": decision,
                "producto": req.producto,
                "stock_anterior": prod["stock_actual"],
                "stock_nuevo": nuevo_stock,
                "residual": residual,
                "umbral": umbral,
            },
        )
    else:
        return ToolResponse(
            success=False,
            message=f"[ALERTA] Movimiento SOSPECHOSO. Residual {residual:.1f} excede umbral {umbral:.1f}. Confirmas la insercion de {req.cantidad} {req.producto}?",
            data={
                "movimiento_id": mov["id"],
                "decision": decision,
                "producto": req.producto,
                "cantidad_reportada": req.cantidad,
                "residual": residual,
                "umbral": umbral,
                "puntaje_riesgo": abs(residual) / umbral,
            },
        )


@app.post("/tool/remover_inventario", response_model=ToolResponse)
def remover_inventario(req: RemoverRequest):
    if req.cantidad <= 0:
        raise HTTPException(400, "La cantidad debe ser positiva")

    prod = db.query_one(
        "SELECT id, nombre, stock_actual FROM productos WHERE nombre = %s",
        (req.producto,),
    )
    if not prod:
        return ToolResponse(
            success=False,
            message=f"Producto '{req.producto}' no encontrado.",
        )

    if prod["stock_actual"] < req.cantidad:
        return ToolResponse(
            success=False,
            message=f"Stock insuficiente de {req.producto}: {prod['stock_actual']} disponibles, solicitaste {req.cantidad}",
        )

    db.execute(
        "INSERT INTO inventario_movimientos (producto_id, tipo, cantidad_reportada) VALUES (%s, 'salida', %s)",
        (prod["id"], req.cantidad),
    )

    mov = db.query_one(
        "SELECT id, decision_kalman, residual_kalman, umbral_usado, stock_resultante FROM inventario_movimientos WHERE producto_id = %s ORDER BY id DESC LIMIT 1",
        (prod["id"],),
    )

    decision = mov["decision_kalman"]
    residual = mov["residual_kalman"]
    umbral = mov["umbral_usado"]
    nuevo_stock = mov["stock_resultante"]

    if decision == "ACEPTADA":
        return ToolResponse(
            success=True,
            message=f"Movimiento ACEPTADO por Kalman. {req.producto}: {prod['stock_actual']} -> {nuevo_stock} | residual: {residual:.1f}s",
            data={
                "movimiento_id": mov["id"],
                "decision": decision,
                "producto": req.producto,
                "stock_anterior": prod["stock_actual"],
                "stock_nuevo": nuevo_stock,
                "residual": residual,
            },
        )
    else:
        return ToolResponse(
            success=False,
            message=f"[ALERTA] Movimiento SOSPECHOSO. Residual {residual:.1f} excede umbral {umbral:.1f}. Confirmas la remocion de {req.cantidad} {req.producto}?",
            data={
                "movimiento_id": mov["id"],
                "decision": decision,
                "producto": req.producto,
                "cantidad_reportada": req.cantidad,
                "residual": residual,
                "umbral": umbral,
                "puntaje_riesgo": abs(residual) / umbral,
            },
        )


@app.post("/tool/consultar_inventario", response_model=ToolResponse)
def consultar_inventario(req: ConsultarRequest):
    if req.producto:
        prod = db.query_one(
            "SELECT nombre, stock_actual, media_kalman, varianza_kalman FROM productos WHERE nombre = %s",
            (req.producto,),
        )
        if not prod:
            return ToolResponse(
                success=False,
                message=f"Producto '{req.producto}' no encontrado.",
            )
        return ToolResponse(
            success=True,
            message=f"{prod['nombre']}: {prod['stock_actual']} unidades | Kalman mu={prod['media_kalman']:.1f} s2={prod['varianza_kalman']:.1f}",
            data=dict(prod),
        )
    else:
        rows = db.query(
            "SELECT nombre, stock_actual, media_kalman, varianza_kalman FROM productos ORDER BY nombre"
        )
        return ToolResponse(
            success=True,
            message=f"Inventario completo ({len(rows)} productos)",
            data=rows,
        )


@app.post("/tool/investigar_sospechosos", response_model=ToolResponse)
def investigar_sospechosos(req: InvestigarRequest):
    rows = db.query(
        "SELECT * FROM investigar_sospechosos(%s::varchar)",
        (req.producto,),
    )

    if not rows:
        return ToolResponse(
            success=True,
            message="No hay movimientos sospechosos registrados.",
            data=[],
        )

    top = rows[0]
    labels = {3: "BAJO", 2: "MEDIO", 1.5: "ALTO", 0: "CRITICO"}
    nivel = "CRITICO"
    for threshold, label in sorted(labels.items()):
        if top["puntaje_riesgo"] > threshold:
            nivel = label
            break

    return ToolResponse(
        success=True,
        message=f"[AUDITORIA] Top sospechoso: #{top['movimiento_id']} | {top['producto_nombre']} {top['tipo']} {top['cantidad_reportada']} | riesgo: {nivel} ({top['puntaje_riesgo']:.1f}s)",
        data={"sospechosos": rows, "mayor_sospechoso": top},
    )


@app.post("/tool/confirmar_movimiento", response_model=ToolResponse)
def confirmar_movimiento(req: ConfirmarRequest):
    rows = db.query(
        "SELECT confirmar_movimiento(%s, %s) AS result",
        (req.movimiento_id, req.confirmar),
    )

    msg = rows[0]["result"] if rows else "Error al procesar la confirmacion"

    return ToolResponse(
        success="confirmado" in msg.lower() or "rechazado" in msg.lower(),
        message=msg,
    )


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
