"""メンバーごとのロール計画と、その適用（API 呼び出し）を扱うモジュール。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

import discord

from companion.config import AppConfig
from companion.grade import grade_number, grade_role_entry

log = logging.getLogger(__name__)

# メンバーを受け取り「最終的に持たせたいロール ID 集合」を返す関数
MemberPlan = Callable[[discord.Member], set[int]]


def current_role_ids(member: discord.Member) -> set[int]:
    """@everyone を除いた現在のロール ID 集合を返す。"""
    return {role.id for role in member.roles if not role.is_default()}


def target_grade_role_id(
    member: discord.Member, config: AppConfig, now: datetime
) -> int | None:
    """メンバーが今持つべき学年ロールの ID を返す。"""
    # 参加日時から学年を算出する
    grade = grade_number(member.joined_at, now, config.timezone)
    # 学年に対応するロール設定を引く
    entry = grade_role_entry(config.grade_roles, grade)
    return entry.role_id if entry else None


def plan_grade_sync(
    member: discord.Member, config: AppConfig, now: datetime
) -> set[int]:
    """学年ロールを 1 つだけ正しいものに揃えたロール集合を返す。"""
    # 既存の学年ロールをすべて外した集合を作る
    target = current_role_ids(member) - config.grade_role_ids
    # 現在の学年に対応するロールだけを付け直す
    role_id = target_grade_role_id(member, config, now)
    if role_id is not None:
        target.add(role_id)
    return target


def plan_join(member: discord.Member, config: AppConfig, now: datetime) -> set[int]:
    """新規加入時のロール集合（加入ロール + 学年ロール）を返す。"""
    # 学年ロールを揃えたうえで加入ロールを足す
    return plan_grade_sync(member, config, now) | config.join_role_ids


def plan_add(member: discord.Member, role_ids: frozenset[int]) -> set[int]:
    """指定ロールを追加したロール集合を返す。"""
    return current_role_ids(member) | role_ids


def plan_remove(member: discord.Member, role_ids: frozenset[int]) -> set[int]:
    """指定ロールを除いたロール集合を返す。"""
    return current_role_ids(member) - role_ids


def is_manageable(guild: discord.Guild, role_id: int) -> bool:
    """Bot がそのロールを付け外しできるかを返す。"""
    role = guild.get_role(role_id)
    # 存在し、管理ロールでなく、Bot の最上位ロールより下である必要がある
    return role is not None and role.is_assignable()


async def apply_role_ids(
    member: discord.Member, target_ids: set[int], reason: str
) -> bool:
    """ロール集合を 1 回の API 呼び出しで適用する。変更が無ければ呼ばない。

    Returns:
        API を呼び出した（変更があった）場合 True。
    """
    guild = member.guild
    current = current_role_ids(member)
    # Bot が操作できるロールだけを差分の対象にする
    to_add = {rid for rid in target_ids - current if is_manageable(guild, rid)}
    to_remove = {rid for rid in current - target_ids if is_manageable(guild, rid)}
    # 差分が無ければ API を呼ばずに終了する（レート制限の節約）
    if not to_add and not to_remove:
        return False
    # 付け外しを 1 回の PATCH にまとめるため最終的なロール一覧を作る
    final_ids = (current | to_add) - to_remove
    roles = [role for rid in final_ids if (role := guild.get_role(rid)) is not None]
    await member.edit(roles=roles, reason=reason)
    log.debug("ロール更新: %s +%s -%s", member, to_add, to_remove)
    return True
