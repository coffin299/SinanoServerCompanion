"""レート制限を考慮しながら全メンバーのロールを順次更新する実行器。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import discord

from companion.roles import MemberPlan, apply_role_ids

log = logging.getLogger(__name__)

# 進捗表示を更新するためのコールバック
ProgressCallback = Callable[[], Awaitable[None]]


@dataclass
class JobProgress:
    """一括処理の進捗と結果。"""

    title: str
    total: int
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    processed: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    cancelled: bool = False
    finished_at: datetime | None = None


class BulkRoleRunner:
    """一括処理を同時に 1 つだけ実行する実行器。"""

    def __init__(self) -> None:
        # 実行枠を確保済みかどうか（取得〜実行完了まで True）
        self._busy = False
        # 中止要求を伝えるイベント
        self._cancel_event = asyncio.Event()
        # 実行中ジョブと直近の完了ジョブ
        self.current: JobProgress | None = None
        self.last: JobProgress | None = None

    @property
    def busy(self) -> bool:
        """一括処理の実行枠が使用中かどうか。"""
        return self._busy

    def try_claim(self) -> bool:
        """実行枠を同期的に確保する。既に使用中なら False。"""
        # 二重実行を防ぐため await を挟まずに判定と確保を行う
        if self._busy:
            return False
        self._busy = True
        # 前回の中止要求を持ち越さない
        self._cancel_event.clear()
        return True

    def release(self) -> None:
        """実行枠を解放する。"""
        self._busy = False

    def cancel(self) -> bool:
        """実行中の処理に中止を要求する。実行中でなければ False。"""
        if not self._busy:
            return False
        self._cancel_event.set()
        return True

    async def run(
        self,
        title: str,
        members: Sequence[discord.Member],
        plan: MemberPlan,
        reason: str,
        api_delay: float,
        progress_interval: float,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        """確保済みの実行枠で全メンバーを順に処理する。"""
        progress = JobProgress(title=title, total=len(members))
        self.current = progress
        # 開始直後に一度表示を更新する
        await self._notify(on_progress)
        last_report = time.monotonic()
        try:
            for member in members:
                # 中止要求があればループを抜ける
                if self._cancel_event.is_set():
                    progress.cancelled = True
                    break
                called_api = await self._process(member, plan, reason, progress)
                progress.processed += 1
                # API を呼んだときだけ待機してレート制限に余裕を持たせる
                if called_api:
                    await asyncio.sleep(api_delay)
                # 進捗表示の更新自体も API なので一定間隔に間引く
                if time.monotonic() - last_report >= progress_interval:
                    await self._notify(on_progress)
                    last_report = time.monotonic()
        finally:
            # 例外・中止でも結果を確定させて「直近の結果」に移す
            progress.finished_at = discord.utils.utcnow()
            self.last = progress
            self.current = None
        log.info(
            "%s 終了: 変更 %d / 変更なし %d / 失敗 %d%s",
            title,
            progress.changed,
            progress.unchanged,
            progress.failed,
            "（中止）" if progress.cancelled else "",
        )
        return progress

    @staticmethod
    async def _process(
        member: discord.Member,
        plan: MemberPlan,
        reason: str,
        progress: JobProgress,
    ) -> bool:
        """1 人分を処理し、API を呼び出したかどうかを返す。"""
        try:
            changed = await apply_role_ids(member, plan(member), reason)
        except discord.HTTPException as exc:
            # 権限不足や退出済みなどは失敗として数えて続行する
            log.warning("ロール更新に失敗: %s (%s)", member, exc)
            progress.failed += 1
            return True
        # 変更の有無で集計先を分ける
        if changed:
            progress.changed += 1
        else:
            progress.unchanged += 1
        return changed

    @staticmethod
    async def _notify(on_progress: ProgressCallback | None) -> None:
        """進捗コールバックを呼ぶ。表示の失敗で処理は止めない。"""
        if on_progress is None:
            return
        try:
            await on_progress()
        except Exception:  # noqa: BLE001
            log.exception("進捗表示の更新に失敗しました")
