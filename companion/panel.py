"""Components V2 を使った管理パネル UI。"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import discord

from companion import actions
from companion.actions import BulkAction
from companion.config import ConfigError, RoleEntry
from companion.notify import plain_name
from companion.roles import is_manageable

if TYPE_CHECKING:
    from companion.bot import CompanionBot

# 永続ビュー用の custom_id（再起動後もボタンを有効にするため固定値）
CID_PREFIX = "sinano:panel:"
CID_GRADE_SYNC = CID_PREFIX + "grade_sync"
CID_JOIN_ADD = CID_PREFIX + "join_add"
CID_GRADE_REMOVE = CID_PREFIX + "grade_remove"
CID_JOIN_REMOVE = CID_PREFIX + "join_remove"
CID_CUSTOM = CID_PREFIX + "custom"
CID_CANCEL = CID_PREFIX + "cancel"
CID_RELOAD = CID_PREFIX + "reload"

# 表示まわりの定数
PANEL_COLOUR = discord.Colour.blurple()
DANGER_COLOUR = discord.Colour.red()
PROGRESS_BAR_LENGTH = 12
CONFIRM_TIMEOUT = 60.0
CUSTOM_TIMEOUT = 180.0
CUSTOM_MAX_ROLES = 10

# ボタン押下時に呼ばれるコールバックの型
ButtonCallback = Callable[[discord.Interaction], Awaitable[None]]


def is_allowed_user(interaction: discord.Interaction) -> bool:
    """操作者が設定のホワイトリストに含まれるかを返す。"""
    bot = cast("CompanionBot", interaction.client)
    return interaction.user.id in bot.config.allowed_user_ids


def notice_view(text: str, colour: discord.Colour = PANEL_COLOUR) -> discord.ui.LayoutView:
    """テキストだけの通知用 LayoutView を作る。"""
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=colour))
    return view


async def check_permission(interaction: discord.Interaction) -> bool:
    """ホワイトリスト外なら拒否メッセージを出して False を返す。"""
    if is_allowed_user(interaction):
        return True
    await interaction.response.send_message(
        view=notice_view("⛔ この操作は許可されたユーザーのみ使えます。", DANGER_COLOUR)
    )
    return False


def make_button(
    label: str,
    emoji: str,
    style: discord.ButtonStyle,
    callback: ButtonCallback,
    custom_id: str | None = None,
    disabled: bool = False,
) -> discord.ui.Button:
    """コールバック付きのボタンを作る。"""
    button = discord.ui.Button(
        label=label, emoji=emoji, style=style, custom_id=custom_id, disabled=disabled
    )
    # インスタンス属性で上書きしてボタンごとの処理を割り当てる
    button.callback = callback
    return button


def _progress_bar(done: int, total: int) -> str:
    """テキストの進捗バーを返す。"""
    # 対象 0 人なら満タン扱いにする
    ratio = 1.0 if total == 0 else done / total
    filled = round(ratio * PROGRESS_BAR_LENGTH)
    return "▰" * filled + "▱" * (PROGRESS_BAR_LENGTH - filled)


def _role_line(guild: discord.Guild | None, label: str, entry: RoleEntry) -> str:
    """ロール設定 1 件の表示行を作る（操作不可なら警告マーク付き）。"""
    # サーバー未取得または操作不可のロールには ⚠️ を付ける
    ok = guild is not None and is_manageable(guild, entry.role_id)
    return f"- {label}<@&{entry.role_id}>{'' if ok else ' ⚠️'}"


def _roles_text(bot: CompanionBot) -> str:
    """設定中のロール一覧を表示用テキストにする。"""
    config = bot.config
    guild = bot.get_guild(config.guild_id)
    lines = ["### 🏷️ 新規加入時に付けるロール"]
    # 加入ロールは名前どおりに並べる
    lines += [_role_line(guild, "", entry) for entry in config.join_roles] or ["- （なし）"]
    lines.append("### 🎓 学年ロール（参加年数）")
    last_index = len(config.grade_roles) - 1
    for index, entry in enumerate(config.grade_roles):
        # 最後の段は「以降」を付けて上限であることを示す
        suffix = "以降" if index == last_index else ""
        lines.append(_role_line(guild, f"{index + 1}年目{suffix}: ", entry))
    lines.append("-# ⚠️ = ロールが見つからない、または Bot より上位のため操作できません")
    return "\n".join(lines)


def _status_text(bot: CompanionBot) -> str:
    """実行状況と前回結果を表示用テキストにする。"""
    runner = bot.runner
    lines = ["### 📊 状態"]
    current = runner.current
    # 実行中・準備中・待機中で表示を分ける
    if current is not None:
        bar = _progress_bar(current.processed, current.total)
        lines.append(f"🔄 **実行中: {current.title}**")
        lines.append(f"`{bar}` {current.processed} / {current.total} 人")
        lines.append(current.summary())
    elif runner.busy:
        lines.append("⏳ メンバー一覧を取得中…")
    else:
        lines.append("⏸️ 待機中")
    last = runner.last
    # 完了済みの前回結果があれば相対時刻付きで添える
    if last is not None and last.finished_at is not None:
        result = "⛔ 中止" if last.cancelled else "✅ 完了"
        stamp = int(last.finished_at.timestamp())
        lines.append(f"-# 前回: {last.title} {result} <t:{stamp}:R>（{last.summary()}）")
    return "\n".join(lines)


def _footer_text(bot: CompanionBot) -> str:
    """動作設定の要約を返す。"""
    config = bot.config
    # 0 時間は定期同期無効を意味する
    sync = (
        f"{config.sync_interval_hours:g} 時間ごと"
        if config.sync_interval_hours > 0
        else "無効"
    )
    return (
        f"-# 定期同期: {sync} ／ API 操作間隔: {config.api_delay_seconds:g} 秒 ／ "
        f"タイムゾーン: {config.timezone.key}"
    )


class AdminPanel(discord.ui.LayoutView):
    """チャンネルに常設する管理パネル（永続ビュー）。"""

    def __init__(self, bot: CompanionBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        busy = bot.runner.busy
        no_join = not bot.config.join_roles
        primary = discord.ButtonStyle.primary
        danger = discord.ButtonStyle.danger
        secondary = discord.ButtonStyle.secondary

        container = discord.ui.Container(accent_colour=PANEL_COLOUR)
        container.add_item(discord.ui.TextDisplay("## 🎓 学年ロール管理パネル"))
        container.add_item(discord.ui.TextDisplay(_roles_text(bot)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_status_text(bot)))
        container.add_item(discord.ui.Separator())
        # 付与系の操作（実行中は押せないようにする）
        container.add_item(
            discord.ui.ActionRow(
                make_button(
                    "学年ロール同期", "🔁", discord.ButtonStyle.success,
                    self._on_grade_sync, CID_GRADE_SYNC, busy,
                ),
                make_button(
                    "加入ロール一括付与", "➕", primary,
                    self._on_join_add, CID_JOIN_ADD, busy or no_join,
                ),
                make_button(
                    "任意ロール一括操作", "🧰", primary,
                    self._on_custom, CID_CUSTOM, busy,
                ),
            )
        )
        # 削除系の操作
        container.add_item(
            discord.ui.ActionRow(
                make_button(
                    "学年ロール一括削除", "🗑️", danger,
                    self._on_grade_remove, CID_GRADE_REMOVE, busy,
                ),
                make_button(
                    "加入ロール一括削除", "➖", danger,
                    self._on_join_remove, CID_JOIN_REMOVE, busy or no_join,
                ),
            )
        )
        # 制御系の操作（中止は実行中のみ有効）
        container.add_item(
            discord.ui.ActionRow(
                make_button("中止", "⏹️", secondary, self._on_cancel, CID_CANCEL, not busy),
                make_button("表示更新 / 設定再読込", "♻️", secondary, self._on_reload, CID_RELOAD),
            )
        )
        container.add_item(discord.ui.TextDisplay(_footer_text(bot)))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """管理権限を持つ人だけが操作できるようにする。"""
        return await check_permission(interaction)

    async def _open_confirm(self, interaction: discord.Interaction, action: BulkAction) -> None:
        """確認ダイアログを開く。"""
        # 進捗表示先としてこのパネルを覚えておく
        self.bot.remember_panel(interaction.message)
        # 実行中なら確認を出さずに知らせる
        if self.bot.runner.busy:
            await interaction.response.send_message(
                view=notice_view("⏳ 他の一括処理が実行中です。")
            )
            return
        await interaction.response.send_message(view=ConfirmView(self.bot, action))

    async def _on_grade_sync(self, interaction: discord.Interaction) -> None:
        await self._open_confirm(interaction, actions.grade_sync_action())

    async def _on_join_add(self, interaction: discord.Interaction) -> None:
        await self._open_confirm(interaction, actions.join_roles_add_action())

    async def _on_grade_remove(self, interaction: discord.Interaction) -> None:
        await self._open_confirm(interaction, actions.grade_roles_remove_action())

    async def _on_join_remove(self, interaction: discord.Interaction) -> None:
        await self._open_confirm(interaction, actions.join_roles_remove_action())

    async def _on_custom(self, interaction: discord.Interaction) -> None:
        """任意ロール選択ダイアログを開く。"""
        self.bot.remember_panel(interaction.message)
        await interaction.response.send_message(view=CustomRoleView(self.bot))

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """実行中の一括処理を中止する。"""
        self.bot.remember_panel(interaction.message)
        # 実行中でなければ知らせるだけ
        if not self.bot.runner.cancel():
            await interaction.response.send_message(
                view=notice_view("実行中の処理はありません。")
            )
            return
        await interaction.response.edit_message(view=AdminPanel(self.bot))
        # 応答を返した後で一括操作ログに残す（3 秒以内の応答期限を優先）
        await self.bot.notifier.bulk_log(
            [f"⏹️ {plain_name(interaction.user)} さんが実行中の一括処理を中止しました"]
        )

    async def _on_reload(self, interaction: discord.Interaction) -> None:
        """設定ファイルを読み直してパネルを描き直す。"""
        self.bot.remember_panel(interaction.message)
        try:
            await self.bot.reload_config()
        except ConfigError as exc:
            # 読み込み失敗時は旧設定のまま理由を表示する
            await interaction.response.send_message(
                view=notice_view(f"❌ 設定の再読込に失敗しました。\n```\n{exc}\n```", DANGER_COLOUR)
            )
            return
        await interaction.response.edit_message(view=AdminPanel(self.bot))
        await self.bot.notifier.system_log(
            [f"♻️ {plain_name(interaction.user)} さんが設定を再読込しました"]
        )


class ConfirmView(discord.ui.LayoutView):
    """一括処理の実行確認ダイアログ。"""

    def __init__(self, bot: CompanionBot, action: BulkAction) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.bot = bot
        self.action = action
        count = bot.target_member_count()
        # 変更が必要な人数ぶんだけ待機が入るため最大所要時間の目安を出す
        minutes = math.ceil(count * bot.config.api_delay_seconds / 60)
        icon = "⚠️" if action.destructive else "❓"
        text = (
            f"### {icon} {action.title}\n"
            f"{action.description}\n"
            f"対象: **{count} 人**\n"
            f"-# 変更が必要な人だけ {bot.config.api_delay_seconds:g} 秒間隔で順に処理します"
            f"（最大でも約 {minutes} 分）。"
        )
        style = discord.ButtonStyle.danger if action.destructive else discord.ButtonStyle.success
        colour = DANGER_COLOUR if action.destructive else PANEL_COLOUR
        container = discord.ui.Container(accent_colour=colour)
        container.add_item(discord.ui.TextDisplay(text))
        container.add_item(
            discord.ui.ActionRow(
                make_button("実行する", "▶️", style, self._on_confirm),
                make_button("キャンセル", "✖️", discord.ButtonStyle.secondary, self._on_cancel),
            )
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_permission(interaction)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        """一括処理を開始する。"""
        self.stop()
        # 実行枠を確保できたかで表示を変える
        if self.bot.start_bulk(self.action, actor=interaction.user):
            text = f"▶️ 「{self.action.title}」を開始しました。進捗はパネルに表示されます。"
        else:
            text = "⏳ 他の一括処理が実行中のため開始できませんでした。"
        await interaction.response.edit_message(view=notice_view(text))

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=notice_view("キャンセルしました。"))


class CustomRoleView(discord.ui.LayoutView):
    """任意のロールを選んで全員に付与 / 削除するダイアログ。"""

    def __init__(self, bot: CompanionBot) -> None:
        super().__init__(timeout=CUSTOM_TIMEOUT)
        self.bot = bot
        self.selected: list[discord.Role] = []
        self.select = discord.ui.RoleSelect(
            placeholder=f"対象ロールを選択（最大 {CUSTOM_MAX_ROLES} 個）",
            min_values=1,
            max_values=CUSTOM_MAX_ROLES,
        )
        self.select.callback = self._on_select
        container = discord.ui.Container(accent_colour=PANEL_COLOUR)
        container.add_item(
            discord.ui.TextDisplay(
                "### 🧰 任意ロール一括操作\nロールを選んでから「付与」か「削除」を押してください。"
            )
        )
        container.add_item(discord.ui.ActionRow(self.select))
        container.add_item(
            discord.ui.ActionRow(
                make_button("全員に付与", "➕", discord.ButtonStyle.primary, self._on_add),
                make_button("全員から削除", "➖", discord.ButtonStyle.danger, self._on_remove),
            )
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_permission(interaction)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """選択内容を保持する（表示は変えない）。"""
        self.selected = list(self.select.values)
        await interaction.response.defer()

    async def _on_add(self, interaction: discord.Interaction) -> None:
        await self._to_confirm(interaction, add=True)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        await self._to_confirm(interaction, add=False)

    async def _to_confirm(self, interaction: discord.Interaction, add: bool) -> None:
        """選択ロールを検証し、確認ダイアログへ切り替える。"""
        # 未選択なら案内だけ出す
        if not self.selected:
            await interaction.response.send_message(
                view=notice_view("先にロールを選択してください。")
            )
            return
        # Bot が操作できないロールが含まれていれば中断する
        blocked = [role for role in self.selected if not role.is_assignable()]
        if blocked:
            names = "、".join(role.mention for role in blocked)
            await interaction.response.send_message(
                view=notice_view(f"⛔ Bot が操作できないロールがあります: {names}", DANGER_COLOUR)
            )
            return
        action = actions.custom_roles_action(
            role_ids=frozenset(role.id for role in self.selected),
            add=add,
            label="、".join(role.name for role in self.selected),
        )
        self.stop()
        await interaction.response.edit_message(view=ConfirmView(self.bot, action))
