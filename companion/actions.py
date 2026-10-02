"""管理パネルから実行できる一括処理の定義。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from companion.config import AppConfig
from companion.roles import (
    MemberPlan,
    plan_add,
    plan_grade_sync,
    plan_remove,
)


@dataclass(frozen=True)
class BulkAction:
    """一括処理 1 種類分の定義。"""

    title: str
    description: str
    destructive: bool
    # 実行時点の設定と時刻からメンバーごとの計画関数を作る
    make_plan: Callable[[AppConfig, datetime], MemberPlan]


def grade_sync_action() -> BulkAction:
    """全員の学年ロールを参加年数どおりに揃える。"""
    return BulkAction(
        title="学年ロール同期",
        description="参加年数に応じた学年ロールを付け、それ以外の学年ロールを外します。",
        destructive=False,
        make_plan=lambda config, now: partial(plan_grade_sync, config=config, now=now),
    )


def join_roles_add_action() -> BulkAction:
    """全員に加入ロールを付与する。"""
    return BulkAction(
        title="加入ロール一括付与",
        description="「新規加入時必ず付けるロール」を全員に付与します。",
        destructive=False,
        make_plan=lambda config, _now: partial(
            plan_add, role_ids=config.join_role_ids
        ),
    )


def join_roles_remove_action() -> BulkAction:
    """全員から加入ロールを外す。"""
    return BulkAction(
        title="加入ロール一括削除",
        description="「新規加入時必ず付けるロール」を全員から外します。",
        destructive=True,
        make_plan=lambda config, _now: partial(
            plan_remove, role_ids=config.join_role_ids
        ),
    )


def grade_roles_remove_action() -> BulkAction:
    """全員から学年ロールを外す。"""
    return BulkAction(
        title="学年ロール一括削除",
        description="設定されたすべての学年ロールを全員から外します。",
        destructive=True,
        make_plan=lambda config, _now: partial(
            plan_remove, role_ids=config.grade_role_ids
        ),
    )


def custom_roles_action(role_ids: frozenset[int], add: bool, label: str) -> BulkAction:
    """任意に選んだロールを全員に付与、または全員から削除する。"""
    # 付与と削除で使う計画関数を切り替える
    plan_func = plan_add if add else plan_remove
    verb = "付与" if add else "削除"
    return BulkAction(
        title=f"任意ロール一括{verb}",
        description=f"選択したロール（{label}）を全員{'に' if add else 'から'}{verb}します。",
        destructive=not add,
        make_plan=lambda _config, _now: partial(plan_func, role_ids=role_ids),
    )
