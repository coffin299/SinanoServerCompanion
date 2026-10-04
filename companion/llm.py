"""OpenAI 互換 API（NVIDIA NIM 想定）で応答を生成するクライアント。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import openai

from companion.config import LLMConfig
from companion.search import SEARCH_TOOL, SEARCH_TOOL_NAME, web_search

log = logging.getLogger(__name__)

# ツール呼び出しを繰り返す最大回数（無限ループ防止）
MAX_TOOL_ROUNDS = 3
# API の一時的な失敗を同じモデルで再試行する回数（以降は次のモデルへフォールバック）
MAX_RETRIES = 1
# モデルが廃止・存在しないことを示す HTTP ステータス
GONE_STATUSES = frozenset({404, 410})
# 推論モデルが出力する思考過程（応答には含めない）
THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)
THINK_CLOSE_TAG = "</think>"

Message = dict[str, Any]


def clean_reply(text: str | None) -> str:
    """応答から思考過程タグを取り除いて整える。"""
    text = THINK_PATTERN.sub("", text or "")
    # 開始タグが省略され終了タグだけ残る実装もあるため、その手前を捨てる
    if THINK_CLOSE_TAG in text:
        text = text.split(THINK_CLOSE_TAG, 1)[1]
    return text.strip()


class LLMClient:
    """設定に応じて API クライアントを作り直しつつ応答を生成する。"""

    def __init__(self) -> None:
        self._client: openai.AsyncOpenAI | None = None
        # 現在のクライアントを作った接続設定（再読込で変わったら作り直す）
        self._client_key: tuple[str, str, float] | None = None
        # 廃止と判定したモデル（設定の再読込でモデル一覧が変わるまで飛ばす）
        self._gone_models: set[str] = set()
        self._models_key: tuple[str, ...] = ()

    def _get_client(self, config: LLMConfig) -> openai.AsyncOpenAI:
        """接続設定に対応するクライアントを返す。"""
        key = (config.api_key, config.base_url, config.timeout_seconds)
        # 接続設定が変わったときだけ作り直す
        if self._client is None or self._client_key != key:
            self._client = openai.AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=MAX_RETRIES,
            )
            self._client_key = key
            # 接続先が変われば廃止判定もやり直す
            self._gone_models.clear()
        return self._client

    def _candidate_models(self, config: LLMConfig) -> list[str]:
        """今回試すモデルを優先順に返す。"""
        # モデル一覧が書き換えられたら廃止判定をやり直す
        if config.models != self._models_key:
            self._models_key = config.models
            self._gone_models.clear()
        alive = [model for model in config.models if model not in self._gone_models]
        # 全滅と判定済みでも、誤判定に備えて全モデルをもう一度試す
        return alive or list(config.models)

    async def _complete(
        self, config: LLMConfig, messages: list[Message], use_tools: bool
    ) -> Any:
        """優先順にモデルを試し、最初に成功した応答メッセージを返す。"""
        last_error: openai.OpenAIError | None = None
        for model in self._candidate_models(config):
            try:
                return await self._complete_with(config, model, messages, use_tools)
            except openai.APIStatusError as exc:
                last_error = exc
                # 廃止・存在しないモデルは以後の呼び出しでも飛ばす
                if exc.status_code in GONE_STATUSES:
                    self._gone_models.add(model)
                    log.warning("モデル %s は利用できません（%s）。次のモデルを試します", model, exc.status_code)
                else:
                    log.warning("モデル %s でエラー（%s）。次のモデルを試します", model, exc.status_code)
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                # 通信失敗・タイムアウトも次のモデルで救済を試みる
                last_error = exc
                log.warning("モデル %s に接続できません（%s）。次のモデルを試します", model, type(exc).__name__)
        # すべてのモデルが失敗したら最後のエラーを呼び出し元へ伝える
        raise last_error or openai.OpenAIError("試せるモデルがありません")

    async def _complete_with(
        self, config: LLMConfig, model: str, messages: list[Message], use_tools: bool
    ) -> Any:
        """指定モデルでチャット補完を 1 回呼び、応答メッセージを返す。"""
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        # ツールを使う場合だけ定義を渡す
        if use_tools:
            params["tools"] = [SEARCH_TOOL]
            params["tool_choice"] = "auto"
        try:
            response = await self._get_client(config).chat.completions.create(**params)
        except openai.BadRequestError:
            # ツール非対応のモデルでも会話できるよう、ツール無しで再試行する
            if not use_tools:
                raise
            log.warning("モデル %s がツールに対応していない可能性があるため、検索なしで再試行します", model)
            return await self._complete_with(config, model, messages, use_tools=False)
        return response.choices[0].message

    async def _run_tool(self, config: LLMConfig, call: Any) -> str:
        """LLM から要求されたツールを実行し、結果をテキストで返す。"""
        # 未知のツールはエラーとして LLM に返す
        if call.function.name != SEARCH_TOOL_NAME:
            return f"エラー: ツール「{call.function.name}」は使えません。"
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            # 壊れた引数も LLM に伝えて自己修正させる
            return "エラー: ツールの引数が正しい JSON ではありません。"
        return await web_search(str(arguments.get("query", "")), config.search_results)

    async def generate(self, config: LLMConfig, messages: list[Message]) -> str:
        """会話履歴から応答を生成する（必要に応じてウェブ検索を挟む）。"""
        # 呼び出し元の履歴を壊さないよう複製して追記する
        messages = list(messages)
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await self._complete(config, messages, use_tools=config.web_search)
            # ツール要求が無ければそれが最終回答
            if not reply.tool_calls:
                return clean_reply(reply.content)
            # ツール要求を履歴に残し、続けて各ツールの結果を追記する
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": await self._run_tool(config, call),
                    }
                )
        # 上限まで検索を繰り返したら、ツール無しで最終回答をまとめさせる
        reply = await self._complete(config, messages, use_tools=False)
        return clean_reply(reply.content)
