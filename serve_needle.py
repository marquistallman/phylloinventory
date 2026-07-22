import json
import os
import re
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Needle Server", version="1.0.0")

model = None
params = None
tokenizer = None


@app.on_event("startup")
def load_model():
    global model, params, tokenizer
    from needle import (
        SimpleAttentionNetwork,
        generate,
        get_tokenizer,
        load_checkpoint,
    )

    checkpoint_path = os.getenv("CHECKPOINT_PATH", "checkpoints/needle.pkl")

    if not os.path.exists(checkpoint_path):
        logger.info("Checkpoint not found. Downloading from HuggingFace...")
        from huggingface_hub import hf_hub_download

        checkpoint_path = hf_hub_download(
            repo_id="Cactus-Compute/needle",
            filename="needle.pkl",
            local_dir=os.path.dirname(checkpoint_path),
        )
        logger.info("Downloaded to %s", checkpoint_path)

    logger.info("Loading checkpoint from %s ...", checkpoint_path)

    params, config = load_checkpoint(checkpoint_path)
    model = SimpleAttentionNetwork(config)
    tokenizer = get_tokenizer()

    logger.info("Needle model loaded.")


class InferRequest(BaseModel):
    query: str
    tools: str


class ToolCall(BaseModel):
    name: str
    arguments: dict = {}


def _parse_tool_calls(raw: str) -> list[ToolCall]:
    """Parsea tool calls tolerando salida truncada o duplicada del modelo.

    Needle (26M) a veces corta el JSON a mitad o repite la misma call dos
    veces. En vez de descartar todo, se rescata el primer JSON completo.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    objs = []
    try:
        parsed = json.loads(raw)
        objs = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for m in re.finditer(r"[\[{]", raw):
            try:
                obj, _ = decoder.raw_decode(raw, m.start())
            except json.JSONDecodeError:
                continue
            objs = obj if isinstance(obj, list) else [obj]
            break

    calls, seen = [], set()
    for o in objs:
        if not isinstance(o, dict) or "name" not in o:
            continue
        args = o.get("arguments")
        if not isinstance(args, dict):
            args = {}
        key = json.dumps([o["name"], args], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(name=str(o["name"]), arguments=args))
    return calls


class InferResponse(BaseModel):
    tool_calls: list[ToolCall] = []
    raw_output: str = ""


@app.post("/infer", response_model=InferResponse)
def infer(req: InferRequest):
    from needle import generate

    logger.info("Infer: %s", req.query[:80])

    try:
        result = generate(
            model,
            params,
            tokenizer,
            query=req.query,
            tools=req.tools,
            stream=False,
        )
    except Exception as e:
        logger.exception("Generate failed")
        raise HTTPException(500, str(e))

    raw = result if isinstance(result, str) else json.dumps(result, default=str)
    tool_calls = _parse_tool_calls(raw)
    if not tool_calls and raw.strip():
        logger.warning("Could not parse tool calls from: %s", raw[:200])

    logger.info("Tool calls: %s", [(tc.name, tc.arguments) for tc in tool_calls])
    return InferResponse(tool_calls=tool_calls, raw_output=str(result))


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
