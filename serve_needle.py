import json
import os
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
    arguments: dict


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

    tool_calls = []
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, list):
            tool_calls = [ToolCall(**tc) for tc in parsed]
        elif isinstance(parsed, dict):
            tool_calls = [ToolCall(**parsed)]
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Could not parse tool calls from: %s", result[:200])

    logger.info("Tool calls: %s", [(tc.name, tc.arguments) for tc in tool_calls])
    return InferResponse(tool_calls=tool_calls, raw_output=str(result))


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
