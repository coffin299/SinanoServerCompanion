"""スラッシュコマンド定義。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from companion.grade import grade_number, grade_role_entry
from companion.panel import (
    DANGER_COLOUR,
    AdminPanel,
    check_permission,
    notice_view,
)

if TYPE_CHECKING:
    from companion.bot import CompanionBot


class GradeCog(commands.Cog):
    """学年ロール関連のコマンド群。"""

    def __init__(self, bot: CompanionBot) -> None:
        self.bot = bot

    @app_commands.command(name="学年パネル", description="学年ロール管理パネルをこのチャンネルに設置します")
    @app_commands.guild_only()
    async def panel(self, interaction: discord.Interaction) -> None:
        """管理パネルを通常メッセージとして設置する。"""
        # ホワイトリスト外のユーザーは拒否する
        if not await check_permission(interaction):
            return
        # チャンネル送信は時間がかかる場合があるので先に応答を保留する
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        # 送信できないチャンネル種別なら中断する
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(
                view=notice_view("このチャンネルには設置できません。", DANGER_COLOUR), ephemeral=True
            )
            return
        try:
            # Bot トークンで編集できるよう Interaction 応答ではなく通常送信する
            message = await channel.send(view=AdminPanel(self.bot))
        except discord.HTTPException:
            await interaction.followup.send(
                view=notice_view(
                    "パネルを送信できませんでした。Bot の閲覧・送信権限を確認してください。",
                    DANGER_COLOUR,
                ),
                ephemeral=True,
            )
            return
        # 以後の進捗表示先として記憶する
        self.bot.remember_panel(message)
        await interaction.followup.send(view=notice_view("✅ パネルを設置しました。"), ephemeral=True)

    @app_commands.command(name="学年確認", description="サーバー参加日と学年を表示します")
    @app_commands.guild_only()
    @app_commands.rename(member="メンバー")
    @app_commands.describe(member="確認するメンバー（省略時は自分）")
    async def check(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        """指定メンバー（省略時は自分）の学年を本人だけに表示する。"""
        # ホワイトリスト外のユーザーは拒否する
        if not await check_permission(interaction):
            return
        target = member or interaction.user
        # サーバー外のユーザーは参加日が無いので対象外
        if not isinstance(target, discord.Member):
            await interaction.response.send_message(
                view=notice_view("メンバー情報を取得できませんでした。", DANGER_COLOUR), ephemeral=True
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
            f"### 🎓 {target.mention} は **{grade}年生**\n"
            f"参加日: {joined}\n"
            f"対応ロール: {role_text}"
        )
        await interaction.response.send_message(view=notice_view(text), ephemeral=True)
