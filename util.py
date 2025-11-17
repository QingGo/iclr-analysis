import os
import json
import asyncio
from typing import Any, Dict, List, Callable
from dotenv import load_dotenv

try:
    load_dotenv()
except Exception:
    pass

YEAR = os.environ.get("YEAR", "2025")
API_BASE = os.environ.get("OPENREVIEW_API_BASE", "https://api.openreview.net")
VENUE_ID = f"ICLR.cc/{YEAR}/Conference"
USER_AGENT = os.environ.get("USER_AGENT", "iclr-analysis/1.0")
OPENREVIEW_USERNAME = os.environ.get("OPENREVIEW_USERNAME", "")
OPENREVIEW_PASSWORD = os.environ.get("OPENREVIEW_PASSWORD", "")
MODEL_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.environ.get("OPENAI_MODEL", "deepseek-reasoner")
MODEL_KEY = os.environ.get("OPENAI_API_KEY", "")


def ensure_dirs() -> None:
    """Create required data directories for metadata, PDFs, and checkpoints etc."""
    os.makedirs("data", exist_ok=True)
    os.makedirs(f"data/{YEAR}/state", exist_ok=True)
    os.makedirs(f"data/{YEAR}/meta", exist_ok=True)
    os.makedirs(f"data/{YEAR}/raw", exist_ok=True)
    os.makedirs(f"data/{YEAR}/extracted", exist_ok=True)
    os.makedirs(f"reports/{YEAR}", exist_ok=True)

ensure_dirs()

def retry_async(max_attempts: int = 3, exceptions: tuple[type[BaseException], ...] | type[BaseException] | None = Exception, delay: float = 1.0) -> Callable:
    def deco(func: Callable):
        async def wrapper(*args, **kwargs):
            if exceptions is None:
                exc: tuple[type[BaseException], ...] = (Exception,)
            elif isinstance(exceptions, tuple):
                exc = exceptions
            elif isinstance(exceptions, type) and issubclass(exceptions, BaseException):
                exc = (exceptions,)
            else:
                exc = (Exception,)
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exc as e:
                    if attempt >= max_attempts:
                        raise e
                    await asyncio.sleep(delay)
        return wrapper
    return deco

def load_checkpoint(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"completed": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def atomic_write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)

def save_checkpoint(path: str, state: Dict[str, Any]) -> None:
    atomic_write_json(path, state)

def read_jsonl(path: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    return items

def write_jsonl(items: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as wf:
        for it in items:
            wf.write(json.dumps(it, ensure_ascii=False) + "\n")

def get_value(content: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = content.get(key)
    if isinstance(v, dict):
        return v.get("value", default)
    return v if v is not None else default


