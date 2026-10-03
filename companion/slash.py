"""スラッシュコマンド定義。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from companion.grade import grade_number, grade_role_entry
from companion.notify import plain_name
from companion.panel import (
    DANGER_COLOUR,
    AdminPanel,
    check_permission,
    notice_view,
)

if TYPE_CHECKING:
    from companion.bot import CompanionBot

log = logging.getLogger(__name__)


class GradeCog(commands.Cog):
    """学年ロール関連のコマンド群。"""

    def __init__(self, bot: CompanionBot) -> None:
        self.bot = bot

    @app_commands.command(name="学年パネル", description="学年ロール管理パネルをこのチャンネルに設置します")
    @app_commands.guild_only()
    async def panel(self, interaction: discord.Interaction) -> None:
        """管理パネルをコマンドの応答として設置する。"""
        # ホワイトリスト外のユーザーは拒否する
        if not await check_permission(interaction):
            return
        # パネル自体を応答にして、余計な完了メッセージを出さない
        await interaction.response.send_message(view=AdminPanel(self.bot))
        message = await self._resolve_panel_message(interaction)
        # 以後の進捗表示先として記憶する
        self.bot.remember_panel(message)
        await self.bot.notifier.system_log(
            [f"📌 {plain_name(interaction.user)} さんが {message.jump_url} に管理パネルを設置しました"]
        )

    @staticmethod
    async def _resolve_panel_message(interaction: discord.Interaction) -> discord.Message:
        """応答したパネルを、Bot トークンで編集できる通常メッセージとして取得する。"""
        response = await interaction.original_response()
        channel = interaction.channel
        # 応答メッセージのままだと編集に 15 分の期限があるため、チャンネル経由で取得し直す
        if isinstance(channel, discord.abc.Messageable):
            try:
                return await channel.fetch_message(response.id)
            except discord.HTTPException as exc:
                log.warning("パネルを取得し直せませんでした（15 分後に進捗更新が止まります）: %s", exc)
        # 取得できなければ期限付きでも応答メッセージを使う
        return response

    @app_commands.command(name="学年確認", description="サーバー参加日と学年を表示します")
    @app_commands.guild_only()
    @app_commands.rename(member="メンバー")
    @app_commands.describe(member="確認するメンバー（省略時は自分）")
    async def check(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        """指定メンバー（省略時は自分）の学年を表示する。"""
        # ホワイトリスト外のユーザーは拒否する
        if not await check_permission(interaction):
            return
        target = member or interaction.user
        # サーバー外のユーザーは参加日が無いので対象外
        if not isinstance(target, discord.Member):
            await interaction.response.send_message(
                view=notice_view("メンバー情報を取得できませんでした。", DANGER_COLOUR)
            )
            return
        config = self.bot.config
        grade = grade_number(target.joined_at, discord.utils.utcnow(), config.timezone)
        entry = grade_role_entry(config.grade_roles, grade)
        # 参加日は Discord のタイムスタンプ記法で閲覧者のローカル表示にする
        joined = (
            f"<t:{int(target.joined_at.timestamp())}:D>" if target.joined_at else "不明"
        )
        role_text = f"<@&{entry.role_id}>" if entry else "（未設定）"
        text = (
            f"### 🎓 {plain_name(target)} さんは **{grade}年生**\n"
            f"参加日: {joined}\n"
            f"対応ロール: {role_text}"
        )
        await interaction.response.send_message(view=notice_view(text))
