"""Schemas de tools compartidos por needle-service y openrouter-service."""

L1_TOOLS = [
    {"name": "leer_inventario", "description": "Read or query inventory data. Use for: checking stock, consulting, auditing, investigating, listing products.", "parameters": {}},
    {"name": "modificar_inventario", "description": "Modify inventory data. Use for: adding, removing, inserting, deleting, putting products in or out.", "parameters": {}},
]

L2_READ = [
    {"name": "consultar_inventario", "description": "Check stock levels, list products, or query how much of something is available.", "parameters": {}},
    {"name": "investigar_sospechosos", "description": "Audit inventory, find errors, investigate suspicious or flagged movements.", "parameters": {}},
]

L2_WRITE = [
    {"name": "agregar_inventario", "description": "Add, put in, or increase product stock quantity.", "parameters": {}},
    {"name": "remover_inventario", "description": "Remove, take out, subtract, or decrease product stock quantity.", "parameters": {}},
]

L3_ARGS = {
    "agregar_inventario": {
        "name": "agregar_inventario",
        "description": "Add a product quantity to inventory. The user may specify a unit (kilos, litros, unidades, gramos, mililitros).",
        "parameters": {
            "producto": {"type": "string", "description": "Product name.", "required": True},
            "cantidad": {"type": "number", "description": "Quantity to add (can be decimal, e.g. 0.5, 1.25).", "required": True},
            "unidad": {"type": "string", "description": "Unit mentioned by user: kilos, kilogramos, kg, gramos, gr, litros, lts, ml, unidades, unds, cajas, paquetes, sobres, etc.", "required": False},
        },
    },
    "remover_inventario": {
        "name": "remover_inventario",
        "description": "Remove a product quantity from inventory. The user may specify a unit.",
        "parameters": {
            "producto": {"type": "string", "description": "Product name.", "required": True},
            "cantidad": {"type": "number", "description": "Quantity to remove (can be decimal).", "required": True},
            "unidad": {"type": "string", "description": "Unit mentioned by user.", "required": False},
        },
    },
    "consultar_inventario": {
        "name": "consultar_inventario",
        "description": "Check current inventory stock.",
        "parameters": {"producto": {"type": "string", "description": "Product name.", "required": False}},
    },
    "investigar_sospechosos": {
        "name": "investigar_sospechosos",
        "description": "Find suspicious inventory movements.",
        "parameters": {"producto": {"type": "string", "description": "Product name.", "required": False}},
    },
    "confirmar_movimiento": {
        "name": "confirmar_movimiento",
        "description": "Confirm or reject a pending inventory movement.",
        "parameters": {
            "pending_id": {"type": "integer", "description": "Pending evaluation id to resolve.", "required": True},
            "confirmar": {"type": "boolean", "description": "Whether to confirm.", "required": True},
        },
    },
}

TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "agregar_inventario",
            "description": "Agrega una cantidad de un producto al inventario. El usuario puede mencionar unidad (kilos, litros, gramos, mililitros, unidades, cajas).",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Nombre del producto"},
                    "cantidad": {"type": "number", "description": "Cantidad (puede ser decimal: 0.5, 1.25)"},
                    "unidad": {"type": "string", "description": "Unidad mencionada: kilos, kg, gramos, g, litros, lts, ml, unidades, unds, cajas, paquetes"},
                },
                "required": ["producto", "cantidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remover_inventario",
            "description": "Remueve una cantidad de un producto del inventario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Nombre del producto"},
                    "cantidad": {"type": "number", "description": "Cantidad (puede ser decimal)"},
                    "unidad": {"type": "string", "description": "Unidad mencionada por el usuario"},
                },
                "required": ["producto", "cantidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_inventario",
            "description": "Consulta el stock actual.",
            "parameters": {
                "type": "object",
                "properties": {"producto": {"type": "string", "description": "Nombre del producto (opcional)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "investigar_sospechosos",
            "description": "Investiga movimientos sospechosos.",
            "parameters": {
                "type": "object",
                "properties": {"producto": {"type": "string", "description": "Nombre del producto (opcional)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_movimiento",
            "description": "Confirma o rechaza un movimiento pendiente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pending_id": {"type": "integer", "description": "ID del pending en la cola"},
                    "confirmar": {"type": "boolean", "description": "Confirmar o rechazar"},
                },
                "required": ["pending_id", "confirmar"],
            },
        },
    },
]