"""お祝い通知・システムログ・一括操作ログの送信（メンションは一切飛ばさない）。"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import discord

from companion.bulk import JobProgress
from companion.config import MAX_MESSAGE_LENGTH, AppConfig
from companion.grade import grade_number, grade_role_entry
from companion.roles import RoleChange

if TYPE_CHECKING:
    from companion.bot import CompanionBot

log = logging.getLogger(__name__)

# Discord の 1 メッセージあたりの最大文字数
MESSAGE_LIMIT = MAX_MESSAGE_LENGTH
# 連続送信するときの最小間隔（秒、チャンネル単位のレート制限対策）
MIN_SEND_INTERVAL = 1.0
# 1 回の同期で LLM にお祝いを書かせる最大人数（超えた分は定型文、API の使い過ぎ防止）
MAX_LLM_GREETINGS = 10
# お祝い文を LLM で書くときにキャラクター設定へ添える指示
GREETING_SYSTEM_HINT = (
    "これから Discord に投稿するメッセージを、上のキャラクターとして書いてもらいます。"
    "一人称・口調・語尾・性格などのキャラクター設定を必ず守り、"
    "そのキャラクターが自分の言葉で話しているように書いてください。"
    "前置きや説明は付けず、投稿するメッセージ本文だけを出力してください。"
    "@ を使ったメンションは書かないでください。"
)
# 参考として渡す定型文の扱い（口調が定型文に引っ張られないようにする）
GREETING_REFERENCE_NOTE = "以下の定型文は内容の参考です。口調は真似せず、キャラクターらしく書き直してください。"


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
        # 1 行で上限を超える場合は上限ごとに分割して別の行として扱う
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            # 上限を超えるなら今の塊を確定して新しい塊を始める
            if len(candidate) > limit:
                chunks.append(current)
                current = piece
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

    async def system_log(self, lines: list[str]) -> None:
        """システムログチャンネル（設定再読込・定期同期など）へ送る。"""
        await self._send_lines(self.bot.config.system_log_channel_id, lines)

    async def bulk_log(self, lines: list[str]) -> None:
        """一括操作ログチャンネル（手動の一括操作）へ送る。"""
        await self._send_lines(self.bot.config.bulk_log_channel_id, lines)

    async def _compose(
        self, template: str, instruction: str, values: dict[str, Any], use_llm: bool
    ) -> str:
        """お祝い文を作る（LLM で書けなければ定型文を返す）。"""
        fallback = template.format(**values)
        llm = self.bot.config.llm
        # LLM が無効・指示文が空・上限超過なら定型文のまま
        if not (use_llm and llm.enabled and llm.greetings and instruction):
            return fallback
        messages = [
            # キャラクター設定を土台にして、投稿文の書き方を添える
            {"role": "system", "content": f"{llm.system_prompt}\n\n{GREETING_SYSTEM_HINT}"},
            # 定型文は内容の方向性だけ揃え、口調はキャラクターに任せる
            {
                "role": "user",
                "content": f"{instruction.format(**values)}\n\n{GREETING_REFERENCE_NOTE}\n{fallback}",
            },
        ]
        try:
            # お祝いに検索は不要なのでツールは使わない
            written = await self.bot.llm.generate(llm, messages, allow_tools=False)
        except Exception:  # noqa: BLE001
            # 生成に失敗してもお祝い自体は定型文で必ず送る
            log.exception("お祝い文の生成に失敗したため定型文を使います")
            return fallback
        # 空の応答も失敗とみなして定型文にする
        return written or fallback

    async def celebrate(self, changes: Iterable[RoleChange]) -> None:
        """進級した人ごとのお祝いをお祝い通知チャンネルへ送る。"""
        config = self.bot.config
        # メッセージが空なら進級祝いは無効
        if not config.celebrate_message:
            return
        lines = []
        for count, (change, index) in enumerate(find_promotions(changes, config)):
            values = {
                "name": plain_name(change.member),
                "grade": index + 1,
                "role": config.grade_roles[index].name,
            }
            # 大人数のときは上限を超えた分だけ定型文にする
            lines.append(
                await self._compose(
                    config.celebrate_message,
                    config.llm.celebrate_prompt,
                    values,
                    use_llm=count < MAX_LLM_GREETINGS,
                )
            )
        await self._send_lines(config.celebrate_channel_id, lines)

    async def welcome(self, member: discord.Member) -> None:
        """新規加入者への入学祝いをお祝い通知チャンネルへ送る。"""
        config = self.bot.config
        # メッセージが空なら入学祝いは無効
        if not config.welcome_message:
            return
        # 付与した学年ロール（通常は 1 年生）の表示名を差し込む
        grade = grade_number(member.joined_at, discord.utils.utcnow(), config.timezone)
        entry = grade_role_entry(config.grade_roles, grade)
        values = {
            "name": plain_name(member),
            "server": discord.utils.escape_markdown(member.guild.name),
            "role": entry.name if entry else "",
        }
        text = await self._compose(
            config.welcome_message, config.llm.welcome_prompt, values, use_llm=True
        )
        await self._send_lines(config.celebrate_channel_id, [text])

    async def report_job(self, progress: JobProgress, actor: discord.abc.User | None) -> None:
        """一括処理の結果を送る（手動は一括操作ログ、実行者なしの定期同期はシステムログ）。"""
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
        # 手動操作と定期同期で送信先を分ける
        if actor is not None:
            await self.bulk_log(lines)
        else:
            await self.system_log(lines)
