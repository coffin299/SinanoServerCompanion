"""サーバー参加年数から「n年生」を算出する純粋関数群。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from companion.config import RoleEntry


def count_full_years(start: datetime, end: datetime) -> int:
    """start から end までに経過した満年数を返す（暦日ベース）。"""
    # 年の差分を仮の満年数とする
    years = end.year - start.year
    # 今年の参加記念日をまだ迎えていなければ 1 年引く
    if (end.month, end.day) < (start.month, start.day):
        years -= 1
    # 時計ずれ等で未来日時になっても負にならないようにする
    return max(years, 0)


def grade_number(joined_at: datetime | None, now: datetime, tz: ZoneInfo) -> int:
    """参加日時と現在日時から学年（1 始まり）を返す。"""
    # 参加日時が取得できない場合は新入生として扱う
    if joined_at is None:
        return 1
    # 記念日の判定は設定タイムゾーンの暦で行う
    local_joined = joined_at.astimezone(tz)
    local_now = now.astimezone(tz)
    # 満年数 + 1 が学年（参加 1 年未満は 1 年生）
    return count_full_years(local_joined, local_now) + 1


def grade_role_entry(
    grade_roles: tuple[RoleEntry, ...], grade: int
) -> RoleEntry | None:
    """学年に対応するロール設定を返す（設定段数を超えたら最上級）。"""
    # 学年ロールが 1 つも無ければ対象なし
    if not grade_roles:
        return None
    # 設定された段数を超える学年は最後のロールに留める
    index = min(max(grade, 1), len(grade_roles)) - 1
    return grade_roles[index]
