import asyncio
import os
from typing import Optional

import typer

import util # noqa: F401
from crawler import run_crawl
from extractor import run_extract
from analyzer import build_report
from util import YEAR, load_checkpoint, save_checkpoint


app = typer.Typer(help="ICLR 论文分析总流程：依次执行爬取→抽取→分析")


@app.command("pipeline")
def cli_pipeline(
    crawl_limit: Optional[int] = typer.Option(None, help="爬取最大数量，默认不限"),
    crawl_resume: bool = typer.Option(True, help="根据爬取 checkpoint 跳过已完成项"),
    crawl_adaptive: bool = typer.Option(True, help="启用基于错误率的自适应并发"),
    crawl_max_concurrency: int = typer.Option(5, help="爬取最大并发，范围 1–10"),
    crawl_force_refresh: bool = typer.Option(False, help="强制刷新索引，忽略本地缓存"),
    extract_limit: Optional[int] = typer.Option(None, help="抽取最大数量，默认不限"),
    extract_resume: bool = typer.Option(True, help="根据抽取 checkpoint 跳过已完成项"),
    extract_max_concurrency: int = typer.Option(5, help="抽取最大并发，范围 1–50"),
) -> None:
    asyncio.run(
        run_crawl(
            crawl_limit,
            crawl_resume,
            crawl_adaptive,
            crawl_max_concurrency,
            crawl_force_refresh,
        )
    )
    asyncio.run(run_extract(extract_limit, extract_resume, extract_max_concurrency))
    build_report()


def main() -> None:
    app()


@app.command("prune-extract")
def cli_prune_extract() -> None:
    crawl_path = os.path.join("data", YEAR, "state", "crawl_checkpoint.json")
    extract_path = os.path.join("data", YEAR, "state", "extract_checkpoint.json")
    crawl = load_checkpoint(crawl_path)
    extract = load_checkpoint(extract_path)
    allowed = set(crawl.get("completed", []))
    before = list(extract.get("completed", []))
    after = sorted([pid for pid in before if pid in allowed])
    save_checkpoint(extract_path, {"completed": after})
    print(f"kept={len(after)} removed={len(before) - len(after)} -> {extract_path}")

if __name__ == "__main__":
    main()
