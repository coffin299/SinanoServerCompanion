"""お祝い通知と管理ログの送信（メンションは一切飛ばさない）。"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

import discord

from companion.bulk import JobProgress
from companion.config import AppConfig
from companion.roles import RoleChange

if TYPE_CHECKING:
    from companion.bot import CompanionBot

log = logging.getLogger(__name__)

# Discord の 1 メッセージあたりの最大文字数
MESSAGE_LIMIT = 2000
# 連続送信するときの最小間隔（秒、チャンネル単位のレート制限対策）
MIN_SEND_INTERVAL = 1.0


def plain_name(user: discord.abc.User) -> str:
    """表示名をメンション・装飾にならない文字列にする。"""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(user.display_name))


def grade_index(role_ids: frozenset[int], config: AppConfig) -> int | None:
    """ロール集合に含まれる学年ロールの段（0 始まり）を返す。"""
    # 複数持っている異常時は最上位の段を採用する
    indexes = [i for i, entry in enumerate(config.grade_roles) if entry.role_id in role_ids]
    return max(indexes) if indexes else None


def find_promotions(
    changes: Iterable[RoleChange], config: AppConfig
) -> list[tuple[RoleChange, int]]:
    """進級した人と新しい段の組を返す。"""
    promotions = []
    for change in changes:
        old = grade_index(change.before, config)
        new = grade_index(change.after, config)
        # 元の学年ロールがあり、上の段へ上がった人だけを進級とみなす
        # （学年ロール未所持からの初回付与はお祝いしない）
        if old is not None and new is not None and new > old:
            promotions.append((change, new))
    return promotions


def count_new_grades(changes: Iterable[RoleChange], config: AppConfig) -> dict[int, int]:
    """学年ロールが変わった人数を新しい段ごとに数える（上の学年から順）。"""
    counts: Counter[int] = Counter()
    for change in changes:
        new = grade_index(change.after, config)
        # 学年ロールが付いた、または別の段に変わった人だけ数える
        if new is not None and new != grade_index(change.before, config):
            counts[new] += 1
    return dict(sorted(counts.items(), reverse=True))


def pack_lines(lines: Iterable[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """行を文字数上限以内のメッセージにまとめる。"""
    chunks: list[str] = []
    current = ""
    for line in lines:
        # 1 行で上限を超える異常値は切り詰める
        line = line[:limit]
        candidate = f"{current}\n{line}" if current else line
        # 上限を超えるなら今の塊を確定して新しい塊を始める
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    # 最後の塊を確定する
    if current:
        chunks.append(current)
    return chunks


class Notifier:
    """設定されたチャンネルへ通知を送る。"""

    def __init__(self, bot: CompanionBot) -> None:
        self.bot = bot

    async def _resolve(self, channel_id: int | None) -> discord.abc.Messageable | None:
        """チャンネル ID から送信先を取得する（無効・取得失敗なら None）。"""
        # 未設定なら通知しない
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        # キャッシュに無ければ API で取得する
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                log.warning("通知チャンネル %s を取得できません: %s", channel_id, exc)
                return None
        # カテゴリ等の送信できないチャンネルは除外する
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("通知チャンネル %s にはメッセージを送れません", channel_id)
            return None
        return channel

    async def _send_lines(self, channel_id: int | None, lines: list[str]) -> None:
        """行をまとめて、間隔を空けながら送信する。"""
        if not lines:
            return
        channel = await self._resolve(channel_id)
        if channel is None:
            return
        interval = max(self.bot.config.api_delay_seconds, MIN_SEND_INTERVAL)
        for index, chunk in enumerate(pack_lines(lines)):
            # 2 通目以降は間隔を空けてレート制限を避ける
            if index:
                await asyncio.sleep(interval)
            try:
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("通知の送信に失敗しました: %s", exc)
                return

    async def admin_log(self, lines: list[str]) -> None:
        """管理ログチャンネルへ送る。"""
        await self._send_lines(self.bot.config.log_channel_id, lines)

    async def celebrate(self, changes: Iterable[RoleChange]) -> None:
        """進級した人ごとのお祝いをお祝い通知チャンネルへ送る。"""
        config = self.bot.config
        lines = [
            config.celebrate_message.format(
                name=plain_name(change.member),
                grade=index + 1,
                role=config.grade_roles[index].name,
            )
            for change, index in find_promotions(changes, config)
        ]
        await self._send_lines(config.celebrate_channel_id, lines)

    async def report_job(self, progress: JobProgress, actor: discord.abc.User | None) -> None:
        """一括処理の結果を管理ログへ送る（実行者なしは定期同期）。"""
        header = (
            f"🛠️ {plain_name(actor)} さんが「{progress.title}」を実行しました"
            if actor is not None
            else f"⏰ 定期同期「{progress.title}」を実行しました"
        )
        result = "⛔ 中止" if progress.cancelled else "✅ 完了"
        lines = [header, f"結果: {result} ／ {progress.summary()}"]
        # 学年ロールが変わった人数を「n人がa年生」の形で添える
        lines += [
            f"・{count}人が{index + 1}年生になりました！"
            for index, count in count_new_grades(progress.changes, self.bot.config).items()
        ]
        await self.admin_log(lines)
