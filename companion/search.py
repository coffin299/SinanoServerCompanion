"""DuckDuckGo によるウェブ検索ツール（LLM の Function Calling 用）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ddgs import DDGS

log = logging.getLogger(__name__)

# LLM に公開するツール名
SEARCH_TOOL_NAME = "web_search"
# 検索の地域（日本語の結果を優先する）
SEARCH_REGION = "jp-jp"
# 1 回の検索にかける最大秒数
SEARCH_TIMEOUT_SECONDS = 15
# 1 件あたりの概要の最大文字数（トークン節約）
SNIPPET_LIMIT = 300

# OpenAI 互換 API に渡すツール定義
SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL_NAME,
        "description": (
            "DuckDuckGo でウェブ検索し、上位結果のタイトル・URL・概要を返します。"
            "最新の情報や、知らない事柄を調べる必要があるときに使ってください。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索キーワード"},
            },
            "required": ["query"],
        },
    },
}


def _search_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    """同期 API で検索する（イベントループを止めないよう別スレッドで呼ぶ）。"""
    return DDGS(timeout=SEARCH_TIMEOUT_SECONDS).text(
        query, region=SEARCH_REGION, safesearch="moderate", max_results=max_results
    )


def format_results(results: list[dict[str, Any]]) -> str:
    """検索結果を LLM が読みやすい番号付きテキストにする。"""
    # 結果が無いことも LLM に明示して、推測で答えさせない
    if not results:
        return "検索結果が見つかりませんでした。"
    lines = []
    for index, item in enumerate(results, start=1):
        # 長すぎる概要は切り詰めてトークンを節約する
        body = str(item.get("body", ""))[:SNIPPET_LIMIT]
        lines.append(f"[{index}] {item.get('title', '')}\nURL: {item.get('href', '')}\n{body}")
    return "\n\n".join(lines)


async def web_search(query: str, max_results: int) -> str:
    """ウェブ検索を実行し、結果（失敗時はエラー内容）をテキストで返す。"""
    query = query.strip()
    # 空クエリは検索せず LLM に差し戻す
    if not query:
        return "エラー: 検索キーワードが空です。"
    log.info("ウェブ検索: %s", query)
    try:
        # スレッド側が固まっても応答全体が止まらないよう上限時間を設ける
        results = await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, max_results),
            timeout=SEARCH_TIMEOUT_SECONDS + 5,
        )
    except Exception as exc:  # noqa: BLE001
        # 検索失敗は致命的ではないので、LLM に伝えて回答を続けさせる
        log.warning("ウェブ検索に失敗しました: %s", exc)
        return f"エラー: 検索に失敗しました（{type(exc).__name__}）。検索なしで回答してください。"
    return format_results(results)
