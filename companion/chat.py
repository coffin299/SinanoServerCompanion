"""Bot へのメンションに LLM で応答する Cog。"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from companion.config import LLMConfig
from companion.llm import LLMClient, Message
from companion.notify import pack_lines

if TYPE_CHECKING:
    from companion.bot import CompanionBot

log = logging.getLogger(__name__)

# 曜日の表記（datetime.weekday() の 0=月曜 に対応）
WEEKDAYS = "月火水木金土日"
# 定型の返答
EMPTY_MENTION_REPLY = "はい、何か御用でしょうか？"
COOLDOWN_REPLY = "⏳ 少し待ってからもう一度話しかけてください。"
EMPTY_ANSWER_REPLY = "（うまく答えられませんでした…）"
ERROR_REPLY = "⚠️ 応答の生成に失敗しました。しばらくしてからもう一度お試しください。"
# ウェブ検索が使えるときにシステムプロンプトへ添える指示
SEARCH_HINT = (
    "最新の情報や知らない事柄について聞かれたら web_search ツールで調べ、"
    "検索結果を根拠に答えてください。"
)


class ChatCog(commands.Cog):
    """メンションされたら会話の流れを踏まえて応答する。"""

    def __init__(self, bot: CompanionBot) -> None:
        self.bot = bot
        self.llm = LLMClient()
        # ユーザーごとの最終利用時刻（連投による API 消費を抑える）
        self._last_used: dict[int, float] = {}

    def _should_respond(self, message: discord.Message, config: LLMConfig) -> bool:
        """このメッセージに応答すべきかを判定する。"""
        # 無効化中や Bot 同士の会話には反応しない
        if not config.enabled or message.author.bot or self.bot.user is None:
            return False
        # 対象サーバー以外（DM 含む）は無視する
        if message.guild is None or message.guild.id != self.bot.config.guild_id:
            return False
        # Bot 自身が直接メンションされたときだけ反応する（@everyone 等は除外）
        if self.bot.user not in message.mentions:
            return False
        # チャンネル指定が無ければ全チャンネルで応答する
        if not config.channel_ids:
            return True
        # スレッドは親チャンネルの指定でも許可する
        parent_id = getattr(message.channel, "parent_id", None)
        return bool({message.channel.id, parent_id} & config.channel_ids)

    def _check_cooldown(self, user_id: int, config: LLMConfig) -> bool:
        """利用間隔を満たしていれば記録して True を返す。"""
        now = time.monotonic()
        last = self._last_used.get(user_id)
        # 前回から規定時間が経っていなければ断る
        if last is not None and now - last < config.cooldown_seconds:
            return False
        self._last_used[user_id] = now
        return True

    def _clean_text(self, message: discord.Message) -> str:
        """メンションを表示名に置き換え、Bot 宛てのメンションは取り除く。"""
        text = message.clean_content
        me = message.guild.me if message.guild else None
        # clean_content では Bot へのメンションが「@表示名」になるので消す
        if me is not None:
            text = text.replace(f"@{me.display_name}", "")
        return text.strip()

    def _to_api_message(self, message: discord.Message) -> Message:
        """Discord のメッセージを API 用の 1 発言に変換する。"""
        # Bot 自身の発言はアシスタントの発言として扱う
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return {"role": "assistant", "content": message.content}
        # 複数人の会話を区別できるよう発言者名を添える
        return {"role": "user", "content": f"{message.author.display_name}: {self._clean_text(message)}"}

    async def _resolve_parent(self, message: discord.Message) -> discord.Message | None:
        """返信先のメッセージを取得する（取得できなければ None）。"""
        reference = message.reference
        # 返信でなければ会話の遡りはここまで
        if reference is None or reference.message_id is None:
            return None
        # 解決済み・キャッシュ済みなら API を呼ばない
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved
        if reference.cached_message is not None:
            return reference.cached_message
        try:
            return await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            # 削除済み・権限不足なら遡りを打ち切る
            return None

    async def _collect_history(self, message: discord.Message, limit: int) -> list[Message]:
        """返信チェーンを遡って会話履歴を古い順に集める。"""
        history: list[Message] = []
        current = message
        # 設定件数に達するか返信が途切れるまで遡る
        while len(history) < limit:
            parent = await self._resolve_parent(current)
            if parent is None:
                break
            history.append(self._to_api_message(parent))
            current = parent
        # 遡った順（新しい順）なので古い順に並べ直す
        history.reverse()
        return history

    def _system_prompt(self, message: discord.Message, config: LLMConfig) -> str:
        """キャラクター設定に現在日時・場所などの文脈を添える。"""
        now = discord.utils.utcnow().astimezone(self.bot.config.timezone)
        lines = [
            config.system_prompt,
            "",
            f"現在日時: {now:%Y-%m-%d %H:%M}（{WEEKDAYS[now.weekday()]}）",
            f"サーバー: {message.guild.name if message.guild else '不明'}",
            f"チャンネル: #{getattr(message.channel, 'name', '不明')}",
            "ユーザーの発言は「表示名: 本文」の形式で渡されます。",
        ]
        # 検索が使える場合だけ使い方を指示する
        if config.web_search:
            lines.append(SEARCH_HINT)
        return "\n".join(lines)

    async def _send_waiting(
        self, message: discord.Message, config: LLMConfig
    ) -> discord.Message | None:
        """応答待ちのメッセージを返信する（無効・失敗なら None）。"""
        # 文言が空なら待機表示は出さない
        if not config.waiting_message:
            return None
        try:
            return await message.reply(config.waiting_message, mention_author=False)
        except discord.HTTPException as exc:
            # 待機表示に失敗しても応答自体は続ける
            log.warning("待機メッセージの送信に失敗しました: %s", exc)
            return None

    async def _send_first(
        self, message: discord.Message, chunk: str, waiting: discord.Message | None
    ) -> None:
        """最初の 1 通を送る（待機メッセージがあればそれを書き換える）。"""
        if waiting is not None:
            try:
                await waiting.edit(content=chunk)
                return
            except discord.NotFound:
                # 待機メッセージが消されていたら新しく返信し直す
                pass
        # どの発言への応答か分かるよう返信にする
        await message.reply(chunk, mention_author=False)

    async def _send_reply(
        self, message: discord.Message, text: str, waiting: discord.Message | None = None
    ) -> None:
        """応答を 2000 文字ごとに分割し、最初の 1 通だけ返信（または待機表示の書き換え）で送る。"""
        for index, chunk in enumerate(pack_lines(text.splitlines())):
            try:
                if index == 0:
                    await self._send_first(message, chunk, waiting)
                else:
                    await message.channel.send(chunk)
            except discord.HTTPException as exc:
                log.warning("応答の送信に失敗しました: %s", exc)
                return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        config = self.bot.config.llm
        if not self._should_respond(message, config):
            return
        text = self._clean_text(message)
        # 本文が無いメンションには定型文で返す（API を呼ばない）
        if not text:
            await self._send_reply(message, EMPTY_MENTION_REPLY)
            return
        # 連投は API を呼ばずに断る
        if not self._check_cooldown(message.author.id, config):
            await self._send_reply(message, COOLDOWN_REPLY)
            return
        # 生成に時間がかかるので、まず待機中であることを知らせる
        waiting = await self._send_waiting(message, config)
        try:
            messages: list[Message] = [{"role": "system", "content": self._system_prompt(message, config)}]
            messages += await self._collect_history(message, config.history_limit)
            messages.append(self._to_api_message(message))
            log.info("LLM 応答を生成: %s (%d 発言)", message.author, len(messages) - 1)
            # 生成中は「入力中…」も表示する
            async with message.channel.typing():
                answer = await self.llm.generate(config, messages)
        except Exception:  # noqa: BLE001
            # API エラー以外でも待機表示が残り続けないよう、必ずエラー文に書き換える
            log.exception("LLM の応答生成に失敗しました")
            await self._send_reply(message, ERROR_REPLY, waiting)
            return
        await self._send_reply(message, answer or EMPTY_ANSWER_REPLY, waiting)
