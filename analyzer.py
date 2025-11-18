import glob
import json
import os
from collections import Counter
from typing import Any, Dict, List

import pandas as pd
import plotly.express as px
from wordcloud import WordCloud

from util import YEAR

def load_extracted() -> List[Dict[str, Any]]:
    """Load all JSON extraction outputs from `data/extracted/` into memory."""
    items: List[Dict[str, Any]] = []
    for p in glob.glob(os.path.join("data", YEAR, "extracted", "*.json")):
        with open(p, "r", encoding="utf-8") as f:
            items.append(json.load(f))
    return items


def normalize_tokens(values: List[str]) -> List[str]:
    """Lowercase-strip tokens and drop empties for stable counting."""
    out: List[str] = []
    for v in values:
        t = str(v).strip().lower()
        if t:
            out.append(t)
    return out


def keywords_top(items: List[Dict[str, Any]], k: int) -> List[tuple]:
    """Top-k keywords combining metadata and LLM-derived tags."""
    tokens: List[str] = []
    for it in items:
        tokens.extend(normalize_tokens(it.get("keywords", [])))
        tokens.extend(normalize_tokens(it.get("llm_tags_top20", [])))
    c = Counter(tokens)
    return c.most_common(k)


def optimizations_top(items: List[Dict[str, Any]], k: int) -> List[tuple]:
    """Top-k optimization phrases from LLM output."""
    tokens: List[str] = []
    for it in items:
        tokens.extend(normalize_tokens(it.get("llm_optimizations_top10", [])))
    c = Counter(tokens)
    return c.most_common(k)


def make_bar(data: List[tuple], title: str, path: str) -> None:
    labels = [x[0] for x in data]
    counts = [x[1] for x in data]
    df = pd.DataFrame({"label": labels, "count": counts})
    fig = px.bar(df, x="count", y="label", orientation="h", title=title)
    fig.update_layout(yaxis={'categoryorder':'total ascending'})
    fig.write_html(path, include_plotlyjs="cdn")
    img_path = path.replace(".html", ".png")
    fig.write_image(img_path, format="png", scale=2)



def make_wordcloud(tokens: List[str], path: str) -> None:
    freqs = Counter(tokens)
    wc = WordCloud(
        width=1200,
        height=800,
        background_color="white",
        max_font_size=100,
        collocations=False,
        relative_scaling=0.3, # type: ignore
        max_words=100
    ).generate_from_frequencies(freqs)
    wc.to_file(path)


def build_report() -> None:
    """Build a simple static HTML report and charts from extracted data."""
    items = load_extracted()
    index = os.path.join("reports", YEAR, "index.html")
    if not items:
        html = f"""<html><head><meta charset='utf-8'><title>ICLR {YEAR} 分析报告</title></head><body>
        <h1>ICLR {YEAR} 分析报告</h1>
        <p>尚无可用数据，请先运行爬取与提取脚本。</p>
        </body></html>"""
        with open(index, "w", encoding="utf-8") as f:
            f.write(html)
        return
    kw_top = keywords_top(items, 20)
    opt_top = optimizations_top(items, 20)
    make_bar(kw_top, f"ICLR {YEAR} 关键词 Top20", os.path.join("reports", YEAR, "keywords_top20.html"))
    make_bar(opt_top, f"ICLR {YEAR} 优化点 Top20", os.path.join("reports", YEAR, "optimizations_top20.html"))
    tokens = []
    for it in items:
        tokens.extend(normalize_tokens(it.get("keywords", [])))
        tokens.extend(normalize_tokens(it.get("llm_tags_top20", [])))
    if tokens:
        make_wordcloud(tokens, os.path.join("reports", YEAR, "wordcloud.png"))
    else:
        open(os.path.join("reports", YEAR, "wordcloud.png"), "wb").close()
    index = os.path.join("reports", YEAR, "index.html")
    html = f"""<html><head><meta charset='utf-8'><title>ICLR {YEAR} 分析报告</title></head><body>
    <h1>ICLR {YEAR} 分析报告</h1>
    <h2>关键词云图</h2>
    <img src='wordcloud.png' style='max-width:100%;height:auto;' />
    <h2>关键词 Top20</h2>
    <iframe src='keywords_top20.html' width='100%' height='600' frameborder='0'></iframe>
    <h2>优化点 Top20</h2>
    <iframe src='optimizations_top20.html' width='100%' height='600' frameborder='0'></iframe>
    </body></html>"""
    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    md = f"""# ICLR {YEAR} 分析报告

## 关键词云图
![关键词云图](wordcloud.png)

## 关键词 Top20
![关键词 Top20](keywords_top20.png)

## 优化点 Top20
![优化点 Top20](optimizations_top20.png)
"""
    with open(os.path.join("reports", YEAR, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)


def main() -> None:
    """Script entry to generate the report from extracted data."""
    build_report()


if __name__ == "__main__":
    main()