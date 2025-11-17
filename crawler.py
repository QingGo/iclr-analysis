import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
import typer
from openreview import api as orapi
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TimeElapsedColumn,
)
from util import (
    atomic_write_json,
    atomic_write_bytes,
    save_checkpoint,
    load_checkpoint,
    read_jsonl,
    write_jsonl,
    get_value,
    VENUE_ID,
    USER_AGENT,
    OPENREVIEW_USERNAME,
    OPENREVIEW_PASSWORD,
    YEAR,
    retry_async,
)

SCORE_THRESHOLD_2026 = 6

async def mark_completed_async(
    state: Dict[str, Any],
    pid: str,
    checkpoint_path: str,
    lock: Optional[asyncio.Lock] = None,
) -> None:
    if lock is not None:
        async with lock:
            completed = set(state.get("completed", []))
            completed.add(pid)
            state["completed"] = sorted(list(completed))
            save_checkpoint(checkpoint_path, state)
    else:
        completed = set(state.get("completed", []))
        completed.add(pid)
        state["completed"] = sorted(list(completed))
        save_checkpoint(checkpoint_path, state)


def build_full_index_v2() -> List[Dict[str, Any]]:
    client_v2 = orapi.OpenReviewClient(
        baseurl="https://api2.openreview.net",
        username=OPENREVIEW_USERNAME,
        password=OPENREVIEW_PASSWORD,
    )
    venue_group = client_v2.get_group(VENUE_ID)
    submission_name = (
        (venue_group.content or {})
        .get("submission_name", {})
        .get("value", "Submission")
    )
    submission_invitation = f"{VENUE_ID}/-/{submission_name}"
    if YEAR == "2026":
        submission_notes = client_v2.get_all_notes(invitation=submission_invitation)
        reply_type = "Official_Review"  # also: "Meta_Review","Official_Comment", "Decision", "Rebuttal" etc.
        submissions = client_v2.get_all_notes(
            invitation=submission_invitation, details="replies"
        )
        replies = [
            reply
            for submission in submissions
            for reply in submission.details["replies"]
            if any(
                invitation.endswith(reply_type) for invitation in reply["invitations"]
            )
        ]
        # group by forum, and avg ratings, only keep avg ratings above 6
        forum_ratings = {}
        for reply in replies:
            if "rating" in reply["content"]:
                forum = reply["forum"]
                rating = reply["content"]["rating"]["value"]
                if forum not in forum_ratings:
                    forum_ratings[forum] = []
                forum_ratings[forum].append(rating)
        forum_high_ratings = {
            forum: (sum(ratings) / len(ratings), sorted(ratings))
            for forum, ratings in forum_ratings.items()
            if len(ratings) > 0 and sum(ratings) / len(ratings) >= SCORE_THRESHOLD_2026
        }
        decisions_notes = []
        for n in submission_notes:
            if n.forum in forum_high_ratings:
                json_n = n.to_json()
                json_n["ratings"] = forum_high_ratings[n.forum][1]
                json_n["avg_rating"] = forum_high_ratings[n.forum][0]
                decisions_notes.append(json_n)
    else:
        decisions_notes = client_v2.get_all_notes(
            invitation=submission_invitation, content={"venueid": VENUE_ID}
        )
    items: List[Dict[str, Any]] = []
    for n in decisions_notes:
        if hasattr(n, "to_json"):
            items.append(n.to_json())
        elif isinstance(n, dict):
            items.append(n)
        else:
            try:
                items.append(
                    json.loads(
                        json.dumps(n, default=lambda o: getattr(o, "__dict__", str(o)))
                    )
                )
            except Exception:
                items.append({"_python_repr": str(n)})
    return items


def normalize_index_item(note: Dict[str, Any]) -> Dict[str, Any]:
    cid = note.get("id")
    forum = note.get("forum") or cid
    content = note.get("content", {})
    title = get_value(content, "title")
    authors = get_value(content, "authors", [])
    authorids = get_value(content, "authorids", [])
    abstract = get_value(content, "abstract")
    keywords = get_value(content, "keywords", [])
    ratings = get_value(content, "ratings", [])
    avg_rating = get_value(content, "avg_rating", None)
    pdf_url = f"https://openreview.net/pdf?id={forum}"
    return {
        "paper_id": forum or cid,
        "note_id": cid,
        "title": title,
        "authors": authors or [],
        "authorids": authorids or [],
        "abstract": abstract,
        "keywords": keywords or [],
        "pdf_url": pdf_url,
        "venueid": VENUE_ID,
        "ratings": ratings or [],
        "avg_rating": avg_rating,
    }


@retry_async(max_attempts=3)
async def download_one(
    client: httpx.AsyncClient,
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    state: Dict[str, Any],
    checkpoint_path: str,
    lock: Optional[asyncio.Lock] = None,
) -> Tuple[str, bool]:
    # 计算元数据与 PDF 的目标路径
    pid = item["paper_id"]
    meta_path = os.path.join("data", YEAR, "meta", f"{pid}.json")
    pdf_path = os.path.join("data", YEAR, "raw", f"{pid}.pdf")
    # 若两者均已存在，直接标记完成并返回（避免重复 I/O）
    if os.path.exists(meta_path) and os.path.exists(pdf_path):
        await mark_completed_async(state, pid, checkpoint_path, lock)
        return pid, True
    # 使用信号量控制并发，防止过载
    async with semaphore:
        # 写入标准化的元数据（原子写）
        if not os.path.exists(meta_path):
            atomic_write_json(meta_path, item)
        # 拉取 PDF 内容（跟随重定向），失败将抛出异常
        if not os.path.exists(pdf_path):
            r = await client.get(item["pdf_url"], follow_redirects=True)
            r.raise_for_status()
            # 原子写入 PDF，避免部分写入导致损坏
            atomic_write_bytes(pdf_path, r.content)
        # 统一更新 checkpoint（带可选锁）
        await mark_completed_async(state, pid, checkpoint_path, lock)
        return pid, True


async def run_crawl(
    limit: Optional[int],
    resume: bool,
    adaptive: bool,
    max_concurrency: int,
    force_refresh: bool,
) -> None:
    """Coordinate crawling across accepted papers with optional adaptive concurrency."""
    # checkpoint 路径定义与加载
    checkpoint_path = os.path.join("data", YEAR, "state", "crawl_checkpoint.json")
    state = load_checkpoint(checkpoint_path)
    # 已完成集合用于 resume 时跳过
    completed = set(state.get("completed", []))
    # 统一 UA；超时配置在客户端中设定
    headers = {"User-Agent": USER_AGENT}
    # 统一超时/UA/环境信任配置的 httpx 客户端
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0), headers=headers, trust_env=False
    ) as client:
        # 索引文件路径（完整 v2 原始条目与归一化条目）
        full_index_path = os.path.join("data", YEAR, "index_full.jsonl")
        index_path = os.path.join("data", YEAR, "index.jsonl")
        # 优先使用本地缓存，避免重复访问 OpenReview
        if (
            (not force_refresh)
            and os.path.exists(full_index_path)
            and os.path.getsize(full_index_path) > 0
        ):
            full_items = read_jsonl(full_index_path)
        else:
            full_items = build_full_index_v2()
            print(f"v2_full_items={len(full_items)}")
            write_jsonl(full_items, full_index_path)
        # 归一化索引的缓存与重建
        if (
            (not force_refresh)
            and os.path.exists(index_path)
            and os.path.getsize(index_path) > 0
        ):
            items = read_jsonl(index_path)
            print(f"index_cached_items={len(items)}")
        else:
            items: List[Dict[str, Any]] = []
            for n in full_items:
                items.append(normalize_index_item(n))
            write_jsonl(items, index_path)
            print(f"wrote_index_lines={len(items)} -> {index_path}")
        # 仅针对“未完成”的条目应用 limit（limit 表示最多新下载多少条）
        pending_items: List[Dict[str, Any]] = [
            it for it in items if it["paper_id"] not in completed
        ]
        if limit is not None:
            pending_items = pending_items[:limit]
        # 初始并发限制在 1–5；信号量用于控制并行下载
        tasks: List[asyncio.Task] = []
        sem_value = max(1, min(10, max_concurrency))
        semaphore = asyncio.Semaphore(sem_value)
        # 保护 checkpoint 更新的锁（避免竞态）
        lock = asyncio.Lock()
        # 控制器使用的最近错误窗口（0=成功，1=失败）
        error_window: List[int] = []
        task_map: Dict[int, str] = {}
        for item in pending_items:
            t = asyncio.create_task(
                download_one(client, item, semaphore, state, checkpoint_path, lock)
            )
            tasks.append(t)
            task_map[id(t)] = item["paper_id"]
        # 进度条长度为索引总数，初始进度为历史已完成数量
        total_items = len(items)
        initial_completed_in_set = sum(1 for it in items if it["paper_id"] in completed)

        async def controller():
            """Adjust concurrency based on recent error rate if adaptive is enabled."""
            # 自适应并发控制：周期性调整信号量的令牌数
            if not adaptive:
                return
            while any(not t.done() for t in tasks):
                await asyncio.sleep(2.0)
                recent_errors = sum(1 for e in error_window[-20:] if e == 1)
                # 最近20条错误大于3则降并发；无错误则升并发
                if recent_errors > 3 and semaphore._value > 1:
                    semaphore._value = max(1, semaphore._value - 1)
                elif recent_errors == 0 and semaphore._value < max_concurrency:
                    semaphore._value = min(max_concurrency, semaphore._value + 1)
                error_window.clear()

        ctrl_task = asyncio.create_task(controller())
        # 汇总任务结果，同时记录错误标记供控制器使用
        results: List[Tuple[str, bool]] = []
        failed_count = 0
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
                    f"下载进度 {initial_completed_in_set}/{total_items} 失败:{failed_count}",
                    total=total_items,
                )
                completed_count = initial_completed_in_set
                if initial_completed_in_set:
                    progress.advance(task_id, initial_completed_in_set)
                for fut in asyncio.as_completed(tasks):
                    try:
                        r = await fut
                        results.append(r)
                        error_window.append(0)
                    except Exception as e:
                        failed_count += 1
                        pid = task_map.get(id(fut))
                        print(f"下载失败 {pid}: {e}")
                        error_window.append(1)
                    finally:
                        completed_count += 1
                        progress.advance(task_id, 1)
                        progress.update(
                            task_id,
                            description=f"下载进度 {completed_count}/{total_items} 失败:{failed_count}",
                        )
                        progress.refresh()
        else:
            for fut in asyncio.as_completed(tasks):
                try:
                    r = await fut
                    results.append(r)
                    error_window.append(0)
                except Exception as e:
                    pid = task_map.get(id(fut))
                    print(f"下载失败 {pid}: {e}")
                    error_window.append(1)
        # 等待控制器退出
        await ctrl_task


app = typer.Typer(help="ICLR 爬取工具：仅抓取已接收论文并下载 PDF 与元数据")


@app.command("crawl")
def cli_crawl(
    limit: Optional[int] = typer.Option(None, help="最大抓取数量，默认不限"),
    resume: bool = typer.Option(False, help="根据 checkpoint 跳过已完成项"),
    adaptive: bool = typer.Option(True, help="启用基于错误率的自适应并发"),
    max_concurrency: int = typer.Option(10, help="最大并发，范围 1–10"),
    force_refresh: bool = typer.Option(False, help="强制刷新索引，忽略本地缓存"),
) -> None:
    """Typer 命令入口，解析参数并运行异步抓取。"""
    asyncio.run(run_crawl(limit, resume, adaptive, max_concurrency, force_refresh))


def main() -> None:
    """CLI 入口：委托给 Typer 应用。"""
    app()


if __name__ == "__main__":
    main()
