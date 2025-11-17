import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import fitz
import concurrent.futures
import httpx
import typer
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TimeElapsedColumn,
)
from util import (
    atomic_write_json,
    save_checkpoint,
    load_checkpoint,
    read_jsonl,
    MODEL_BASE,
    MODEL_NAME,
    MODEL_KEY,
    USER_AGENT,
    YEAR
)


def _pdf_text_worker(path: str, max_pages: Optional[int] = None) -> str:
    doc = fitz.open(path)
    texts: List[str] = []
    n = doc.page_count
    pmax = min(n, max_pages or n)
    for i in range(pmax):
        page = doc.load_page(i)
        texts.append(str(page.get_text()))
    return "\n".join(texts)

async def pdf_text(path: str, max_pages: Optional[int] = None, ex: Optional[concurrent.futures.ProcessPoolExecutor] = None) -> str:
    loop = asyncio.get_running_loop()
    if ex is None:
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as ex_local:
            return await loop.run_in_executor(ex_local, _pdf_text_worker, path, max_pages)
    return await loop.run_in_executor(ex, _pdf_text_worker, path, max_pages)


def extract_hyperparams(full_text: str) -> Dict[str, Any]:
    hp: Dict[str, Any] = {}
    m = re.search(r"(?i)mini[-\s]?batch size[:\s]*([0-9,]+)", full_text)
    if m:
        try:
            hp["batch_size"] = int(m.group(1).replace(",", ""))
        except Exception:
            hp["batch_size"] = m.group(1)
    m = re.search(r"(?i)optimizer[:\s-]*([A-Za-z]+)", full_text)
    if m:
        hp["optimizer"] = m.group(1)
    m = re.search(r"(?i)learning rate[:\s-]*([0-9\.eE-]+)", full_text)
    if m:
        hp["learning_rate"] = m.group(1)
    m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*million", full_text)
    if m:
        try:
            hp["steps"] = int(float(m.group(1)) * 1000000)
        except Exception:
            hp["steps"] = m.group(1)
    m = re.search(r"(?i)(?:c)?threshold[:\s-]*([0-9\.]+)", full_text)
    if m:
        try:
            hp["threshold"] = float(m.group(1))
        except Exception:
            hp["threshold"] = m.group(1)
    m = re.search(r"ζ[Qq],?\s*ζπ\s*([0-9\.eE-]+)", full_text)
    if m:
        hp["zeta_params"] = m.group(1)
    return hp


def canonical_authors(meta_authors: Any, text: str) -> List[str]:
    """Prefer metadata authors; fall back to named-entity-like patterns in the header."""
    if isinstance(meta_authors, list) and meta_authors:
        return [str(a).strip() for a in meta_authors]
    head = "\n".join(text.splitlines()[:50])
    m = re.findall(r"\b([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)+)\b", head)
    uniq: List[str] = []
    for name in m:
        if name not in uniq:
            uniq.append(name)
    return uniq[:10]


STOPWORDS = {
    "the",
    "and",
    "of",
    "to",
    "for",
    "in",
    "on",
    "with",
    "by",
    "from",
    "at",
    "as",
    "a",
    "an",
    "via",
}


def simplify_phrase(s: str, max_words: int = 3) -> str:
    """Normalize a phrase to up to `max_words` lowercase tokens excluding stopwords."""
    words = re.findall(r"[A-Za-z][A-Za-z\-]+", s.lower())
    filt = [w for w in words if w not in STOPWORDS][:max_words]
    if filt:
        return " ".join(filt)
    s2 = re.sub(r"\s+", " ", str(s)).strip()
    return s2[:60]


def simplify_list(values: List[Any]) -> List[str]:
    """Simplify a list of phrases using `simplify_phrase`, keep top 10."""
    out: List[str] = []
    for v in values:
        out.append(simplify_phrase(str(v), 3))
    return out[:10]


async def call_llm(
    client: httpx.AsyncClient, title: str, abstract: str, text: str
) -> Tuple[List[str], List[str], Dict[str, int]]:
    """Call LLM API to obtain topical tags and optimization phrases from paper content."""
    prompt = (
        '你是资深学术助手。根据论文标题、摘要与正文片段，仅返回一个 JSON 对象：{"tags": [...], "optimizations": [...]}。\n'
        "- tags：不超过20个，均为关键词或短语；\n"
        '- optimizations：不超过10个，必须是论文相较之前工作的优化方向或优化方法；聚焦方法、策略、架构或数据流程的改进；每项为2–3个关键词的短语，避免完整句子与标点，例如 "data augmentation", "multi-task training", "adapter tuning"。\n'
        "- 输出为纯JSON，不包含解释或其他文本。"
    )
    content = f"标题：{title}\n摘要：{abstract}\n正文片段：{text[:4000]}"
    headers = {
        "Authorization": f"Bearer {MODEL_KEY}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
    }
    r = await client.post(
        f"{MODEL_BASE}/chat/completions", headers=headers, json=body, timeout=60.0
    )
    r.raise_for_status()
    data = r.json()
    txt = data["choices"][0]["message"]["content"].strip()
    usage_raw = data.get("usage", {})
    hit = int(usage_raw.get("prompt_cache_hit_tokens", 0) or 0)
    miss = int(usage_raw.get("prompt_cache_miss_tokens", 0) or 0)
    prompt_toks = int(usage_raw.get("prompt_tokens", hit + miss) or 0)
    completion_toks = int(usage_raw.get("completion_tokens", 0) or 0)
    total_toks = int(usage_raw.get("total_tokens", prompt_toks + completion_toks) or 0)
    usage = {
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "prompt_tokens": prompt_toks,
        "completion_tokens": completion_toks,
        "total_tokens": total_toks,
    }
    try:
        obj = json.loads(txt)
        tags = [str(x).strip() for x in obj.get("tags", [])][:20]
        opt = simplify_list(obj.get("optimizations", []))
        return tags, opt, usage
    except Exception:
        return [], [], {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


async def process_one(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    state: Dict[str, Any],
    ex_pool: concurrent.futures.ProcessPoolExecutor,
) -> Tuple[str, bool, Dict[str, int]]:
    """Extract structured information from a single paper and write a JSON record."""
    # 路径与基础元数据准备：如已有输出则直接返回避免重复计算)
    pid = item["paper_id"]
    out_path = os.path.join("data", YEAR, "extracted", f"{pid}.json")
    pdf_path = os.path.join("data", YEAR, "raw", f"{pid}.pdf")
    meta_path = os.path.join("data", YEAR, "meta", f"{pid}.json")
    meta: Dict[str, Any] = item
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, "r", encoding="utf-8"))
        except Exception:
            meta = item
    # 受限并发下进行解析与调用 LLM
    async with semaphore:
        try:
            text = await pdf_text(pdf_path, ex=ex_pool) if os.path.exists(pdf_path) else ""
            # 超参解析：基于正则的启发式方法
            hyperparams = extract_hyperparams(text) if text else {}
            # 作者列表：优先元数据，回退到正文头部的命名实体风格匹配
            authors = canonical_authors(meta.get("authors"), text)
        except Exception as e:
            print(f"Error processing {pid}: {e}")
            text, hyperparams, authors = "", {}, []
        # 可选 LLM：生成标签与优化短语（受 KEY 控制）
        tags: List[str] = []
        optimizations: List[str] = []
        usage: Dict[str, int] = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if MODEL_KEY:
            t, o, u = await call_llm(
                client,
                meta.get("title") or "",
                meta.get("abstract") or "",
                (text[:20000] if text else ""),
            )
            tags = t
            optimizations = o
            usage = u
        # 输出记录：结构化结果聚合
        out = {
            "paper_id": pid,
            "authors": authors,
            "keywords": meta.get("keywords") or [],
            "llm_tags_top20": tags,
            "llm_optimizations_top10": optimizations,
            "rule_hyperparams": hyperparams,
        }
        if meta.get("ratings") is not None:
            out["ratings"] = meta.get("ratings")
        elif item.get("ratings") is not None:
            out["ratings"] = item.get("ratings")
        if meta.get("avg_rating") is not None:
            out["avg_rating"] = meta.get("avg_rating")
        elif item.get("avg_rating") is not None:
            out["avg_rating"] = item.get("avg_rating")
        # 原子写输出 JSON，避免部分写入
        atomic_write_json(out_path, out)
        # 更新提取 checkpoint（去重与排序），便于 resume
        completed = set(state.get("completed", []))
        completed.add(pid)
        state["completed"] = sorted(list(completed))
        save_checkpoint(os.path.join("data", YEAR, "state", "extract_checkpoint.json"), state)
        return pid, True, usage


async def run_extract(limit: Optional[int], resume: bool, max_concurrency: int) -> None:
    """Coordinate extraction across crawled papers with bounded concurrency."""
    items = read_jsonl(os.path.join("data", YEAR, "index.jsonl"))
    # checkpoint 加载与已完成集合统计
    checkpoint_path = os.path.join("data", YEAR, "state", "extract_checkpoint.json")
    state = load_checkpoint(checkpoint_path)
    completed = set(state.get("completed", []))
    # HTTP 客户端统一配置（超时、环境变量信任关闭）
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0), trust_env=False
    ) as client:
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as ex_pool:
            tasks: List[asyncio.Task] = []
            semaphore = asyncio.Semaphore(max(1, min(50, max_concurrency)))
            new_count = 0
            task_map: Dict[int, str] = {}
            for it in items:
                pid = it["paper_id"]
                pdf_path = os.path.join("data", YEAR, "raw", f"{pid}.pdf")
                if resume and pid in completed:
                    continue
                if not os.path.exists(pdf_path):
                    continue
                if limit is not None and new_count >= limit:
                    break
                t = asyncio.create_task(process_one(it, semaphore, client, state, ex_pool))
                tasks.append(t)
                task_map[id(t)] = pid
                new_count += 1
            total_items = len(items)
            initial_completed_in_set = sum(
                1
                for it in items
                if (it.get("paper_id") in completed)
            )
            results: List[Tuple[str, bool, Dict[str, int]]] = []
            total_hit = 0
            total_miss = 0
            total_out = 0
            total_cost = 0.0
            if total_items > 0:
                progress = Progress(
                    TextColumn("{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total}"),
                    TimeElapsedColumn(),
                    TimeRemainingColumn(),
                )
                with progress:
                    task_id = progress.add_task(
                        f"提取进度 {initial_completed_in_set}/{total_items}",
                        total=total_items,
                    )
                    completed_count = initial_completed_in_set
                    if initial_completed_in_set:
                        progress.advance(task_id, initial_completed_in_set)
                    failed_count = 0
                    for fut in asyncio.as_completed(tasks):
                        ok = False
                        try:
                            r = await fut
                            results.append(r)
                            ok = True
                            _, _, u = r
                            total_hit += int(u.get("prompt_cache_hit_tokens", 0) or 0)
                            total_miss += int(u.get("prompt_cache_miss_tokens", 0) or 0)
                            total_out += int(u.get("completion_tokens", 0) or 0)
                            total_cost = (
                                total_hit * 0.2 / 1_000_000
                                + total_miss * 2 / 1_000_000
                                + total_out * 3 / 1_000_000
                            )
                        except Exception as e:
                            failed_count += 1
                            pid = task_map.get(id(fut))
                            print(f"提取失败 {pid}: {e}")
                            ok = False
                        if ok:
                            completed_count += 1
                            progress.advance(task_id, 1)
                            progress.update(
                                task_id,
                                description=(
                                    f"提取进度 {completed_count}/{total_items} 失败:{failed_count} "
                                    f"入:{total_hit + total_miss} 出:{total_out} 费用:¥{total_cost:.4f}"
                                ),
                            )
            else:
                for fut in asyncio.as_completed(tasks):
                    try:
                        await fut
                    except Exception as e:
                        pid = task_map.get(id(fut)) 
                        print(f"提取失败 {pid}: {e}")


app = typer.Typer(help="ICLR 论文信息提取工具：解析 PDF 与元数据生成结构化结果")


@app.command("extract")
def cli_extract(
    limit: Optional[int] = typer.Option(None, help="最大提取数量，默认不限"),
    resume: bool = typer.Option(True, help="根据 checkpoint 跳过已完成项"),
    max_concurrency: int = typer.Option(5, help="最大并发，范围 1–50"),
) -> None:
    """Typer 命令入口，解析参数并运行异步提取。"""
    asyncio.run(run_extract(limit, resume, max_concurrency))


def main() -> None:
    """CLI 入口：委托给 Typer 应用。"""
    app()


if __name__ == "__main__":
    main()
