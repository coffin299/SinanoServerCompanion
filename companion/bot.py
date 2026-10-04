"""Bot 本体: イベント処理・定期同期・一括処理の起動を担う。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from companion import actions
from companion.actions import BulkAction
from companion.bulk import BulkRoleRunner, JobProgress
from companion.chat import ChatCog
from companion.config import AppConfig, load_config
from companion.notify import Notifier
from companion.panel import AdminPanel
from companion.roles import apply_role_ids, plan_join
from companion.slash import GradeCog

log = logging.getLogger(__name__)

# 定期同期が無効な間、設定再読込で有効化されたかを確認する間隔（秒）
DISABLED_POLL_SECONDS = 300
SECONDS_PER_HOUR = 3600


def build_activity(config: AppConfig) -> discord.CustomActivity | None:
    """設定からカスタムステータスを作る（空なら表示しない）。"""
    return discord.CustomActivity(name=config.status_text) if config.status_text else None


class CompanionBot(commands.Bot):
    """学年ロールを管理する Bot。"""

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        # メンバー一覧と加入イベントの取得に Server Members Intent が必要
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # ロールのメンションを表示しても通知が飛ばないようにする
            allowed_mentions=discord.AllowedMentions.none(),
            # 接続直後から設定のステータスを表示する
            activity=build_activity(config),
            status=config.presence,
        )
        self.config = config
        self.config_path = config_path
        self.runner = BulkRoleRunner()
        self.notifier = Notifier(self)
        # 進捗を反映するパネルメッセージ（最後に操作・設置されたもの）
        self._panel_message: discord.Message | None = None
        # 実行中タスクが GC されないよう参照を保持する
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def setup_hook(self) -> None:
        """ログイン直後の初期化。"""
        # 再起動前に設置したパネルのボタンも反応するよう永続登録する
        self.add_view(AdminPanel(self))
        await self.add_cog(GradeCog(self))
        await self.add_cog(ChatCog(self))
        # 対象サーバーにだけコマンドを即時反映する
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("スラッシュコマンドを %d 件同期しました", len(synced))
        self._spawn(self._periodic_sync_loop())

    async def on_message(self, message: discord.Message) -> None:
        # プレフィックスコマンドは無いので処理しない
        # （メンション付き発言が CommandNotFound としてエラーログに出るのを防ぐ）
        return

    async def on_ready(self) -> None:
        log.info("ログインしました: %s", self.user)
        self._validate_roles()

    def _validate_roles(self) -> None:
        """設定ロールの存在と操作可否をログで知らせる。"""
        guild = self.get_guild(self.config.guild_id)
        # Bot が対象サーバーにいなければ何もできない
        if guild is None:
            log.error("サーバー %s が見つかりません。Bot を招待済みか確認してください。", self.config.guild_id)
            return
        for entry in (*self.config.join_roles, *self.config.grade_roles):
            role = guild.get_role(entry.role_id)
            # ID 間違いと権限（ロール順位）不足を区別して警告する
            if role is None:
                log.warning("ロールが見つかりません: %s (%s)", entry.name, entry.role_id)
            elif not role.is_assignable():
                log.warning("Bot より上位、または管理ロールのため操作できません: %s (%s)", entry.name, role.name)

    def _is_target(self, member: discord.Member) -> bool:
        """自動処理の対象メンバーかどうか。"""
        # 対象サーバー以外は無視する
        if member.guild.id != self.config.guild_id:
            return False
        # 設定により Bot アカウントを除外する
        return not (self.config.ignore_bots and member.bot)

    async def on_member_join(self, member: discord.Member) -> None:
        if not self._is_target(member):
            return
        # メンバー審査（ルール同意）待ちの間は付与を保留する
        if member.pending:
            log.info("メンバー審査待ちのため付与を保留: %s", member)
            return
        await self._assign_join_roles(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # メンバー審査を通過した瞬間に加入ロールを付与する
        if before.pending and not after.pending and self._is_target(after):
            await self._assign_join_roles(after)

    async def _assign_join_roles(self, member: discord.Member) -> None:
        """加入ロールと学年ロール（通常は 1 年生）を付与し、入学祝いを送る。"""
        target = plan_join(member, self.config, discord.utils.utcnow())
        try:
            await apply_role_ids(member, target, reason="新規加入: 加入ロールと学年ロールを付与")
            log.info("新規加入ロールを付与: %s", member)
        except discord.HTTPException:
            log.exception("新規加入ロールの付与に失敗: %s", member)
        # ロール付与の成否にかかわらず入学を祝う
        await self.notifier.welcome(member)

    def remember_panel(self, message: discord.Message | None) -> None:
        """進捗を反映するパネルメッセージを更新する。"""
        if message is not None:
            self._panel_message = message

    async def reload_config(self) -> None:
        """設定ファイルを読み直す（トークンとサーバー ID は再起動まで据え置き）。"""
        new_config = load_config(self.config_path)
        self.config = dataclasses.replace(
            new_config, token=self.config.token, guild_id=self.config.guild_id
        )
        log.info("設定を再読込しました")
        self._validate_roles()
        await self.apply_presence()

    async def apply_presence(self) -> None:
        """現在の設定のステータスを反映する。"""
        try:
            await self.change_presence(
                activity=build_activity(self.config), status=self.config.presence
            )
        except (discord.ClientException, discord.HTTPException) as exc:
            # ステータス反映の失敗で再読込自体は失敗させない
            log.warning("ステータスの反映に失敗しました: %s", exc)

    def target_members(self, guild: discord.Guild) -> list[discord.Member]:
        """一括処理の対象メンバー一覧を返す。"""
        return [m for m in guild.members if not (self.config.ignore_bots and m.bot)]

    def target_member_count(self) -> int:
        """確認ダイアログ表示用の対象人数を返す。"""
        guild = self.get_guild(self.config.guild_id)
        return len(self.target_members(guild)) if guild else 0

    def start_bulk(self, action: BulkAction, actor: discord.abc.User) -> bool:
        """手動の一括処理をバックグラウンドで開始する。実行中なら False。"""
        # 同期的に実行枠を確保して二重実行を防ぐ
        if not self.runner.try_claim():
            return False
        self._spawn(self._execute_claimed(action, actor))
        return True

    async def _execute_claimed(
        self, action: BulkAction, actor: discord.abc.User | None = None
    ) -> None:
        """確保済みの実行枠で一括処理を行い、最後に必ず解放して通知する。

        actor が None の場合は定期同期（時間経過による自動処理）として扱う。
        """
        progress: JobProgress | None = None
        try:
            guild = self.get_guild(self.config.guild_id)
            if guild is None:
                log.error("サーバーが見つからないため %s を中止しました", action.title)
                return
            # 全メンバーがキャッシュされていなければ取得する
            if not guild.chunked:
                await self.refresh_panel()
                await guild.chunk()
            # 開始時点の設定・時刻・メンバーで計画を固定する
            config = self.config
            progress = await self.runner.run(
                title=action.title,
                members=self.target_members(guild),
                plan=action.make_plan(config, discord.utils.utcnow()),
                reason=f"一括処理: {action.title}",
                api_delay=config.api_delay_seconds,
                progress_interval=config.progress_interval_seconds,
                on_progress=self.refresh_panel,
            )
        finally:
            self.runner.release()
            # ボタンの有効状態を戻すため最終状態を描画する
            await self.refresh_panel()
        # 通知は実行枠を解放してから送る（送信待ちで次の操作を妨げない）
        if progress is not None:
            await self._notify_job(progress, actor)

    async def _notify_job(self, progress: JobProgress, actor: discord.abc.User | None) -> None:
        """一括処理の結果を通知する。"""
        # 手動操作は毎回、結果を一括操作ログに残す
        if actor is not None:
            await self.notifier.report_job(progress, actor)
            return
        # 時間経過で進級した人だけを個別にお祝いする
        await self.notifier.celebrate(progress.changes)
        # 定期同期は変更・失敗があったときだけシステムログに残す（毎日の空報告を避ける）
        if progress.changed or progress.failed:
            await self.notifier.report_job(progress, None)

    async def refresh_panel(self) -> None:
        """記憶しているパネルを最新状態で描き直す。"""
        message = self._panel_message
        if message is None:
            return
        try:
            await message.edit(view=AdminPanel(self))
        except discord.NotFound:
            # パネルが削除されていたら以後は更新しない
            self._panel_message = None
        except discord.HTTPException as exc:
            log.warning("パネルの更新に失敗しました: %s", exc)

    async def _periodic_sync_loop(self) -> None:
        """起動時と一定間隔ごとに学年ロールを同期する。"""
        await self.wait_until_ready()
        while not self.is_closed():
            hours = self.config.sync_interval_hours
            # 0 は無効。再読込で有効化される可能性があるので監視だけ続ける
            if hours <= 0:
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue
            # 手動処理が実行中なら今回は見送る
            if self.runner.try_claim():
                try:
                    await self._execute_claimed(actions.grade_sync_action())
                except Exception:  # noqa: BLE001
                    log.exception("定期同期に失敗しました")
            else:
                log.info("他の一括処理が実行中のため定期同期を見送りました")
            await asyncio.sleep(hours * SECONDS_PER_HOUR)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """バックグラウンドタスクを起動し、参照と例外ログを管理する。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        # キャンセル以外の例外はログに残す
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("バックグラウンド処理で例外が発生しました", exc_info=exc)
