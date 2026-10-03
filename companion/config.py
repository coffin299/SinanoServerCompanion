"""YAML 設定ファイルの読み込みと検証を行うモジュール。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import yaml

# YAML 上のキー名（利用者が編集する日本語キー）
KEY_TOKEN = "Botトークン"
KEY_GUILD_ID = "サーバーID"
KEY_ALLOWED_USERS = "コマンドを使えるユーザーID"
KEY_STATUS_TEXT = "カスタムステータス"
KEY_PRESENCE = "オンライン状態"
KEY_CELEBRATE_CHANNEL = "お祝い通知チャンネルID"
KEY_SYSTEM_LOG_CHANNEL = "システムログチャンネルID"
KEY_BULK_LOG_CHANNEL = "一括操作ログチャンネルID"
# 旧バージョンの共通ログチャンネル（新キーが無いときの代替として読む）
KEY_LEGACY_LOG_CHANNEL = "管理ログチャンネルID"
KEY_CELEBRATE_MESSAGE = "お祝いメッセージ"
KEY_TIMEZONE = "タイムゾーン"
KEY_IGNORE_BOTS = "Botを対象外にする"
KEY_SYNC_HOURS = "定期同期間隔(時間)"
KEY_API_DELAY = "API操作間隔(秒)"
KEY_PROGRESS_INTERVAL = "進捗更新間隔(秒)"
KEY_JOIN_ROLES = "新規加入時必ず付けるロール"
KEY_GRADE_ROLES = "サーバー参加年数ごとに付けるロール"

# 未記入のまま起動されたことを検出するためのプレースホルダ
PLACEHOLDER_TOKEN = "ここにBotトークンを貼り付け"

# 省略時の既定値
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_IGNORE_BOTS = True
DEFAULT_SYNC_HOURS = 24.0
DEFAULT_API_DELAY = 1.0
DEFAULT_PROGRESS_INTERVAL = 5.0
MIN_PROGRESS_INTERVAL = 2.0
DEFAULT_PRESENCE = "online"

# 「オンライン状態」に書ける値と discord.Status の対応
PRESENCE_MAP = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

# カスタムステータスの最大文字数（Discord の上限）
MAX_STATUS_LENGTH = 128

# お祝いメッセージの既定文と、差し込める項目の検証用サンプル
DEFAULT_CELEBRATE_MESSAGE = "🎉 祝！{name}さんが{grade}年生になりました！"
CELEBRATE_SAMPLE = {"name": "名無し", "grade": 2, "role": "二年生"}


class ConfigError(Exception):
    """設定ファイルの内容に問題がある場合に送出される例外。"""


class _UniqueKeyLoader(yaml.SafeLoader):
    """重複キーをエラーにする SafeLoader（同名ロールの上書き事故を防ぐ）。"""

    def construct_mapping(self, node, deep=False):
        # 同一マッピング内で出現したキーを記録する
        seen = set()
        for key_node, _ in node.value:
            # キーを Python オブジェクトへ変換して比較する
            key = self.construct_object(key_node, deep=deep)
            # 既出キーなら行番号付きでエラーにする
            if key in seen:
                line = key_node.start_mark.line + 1
                raise ConfigError(
                    f"YAML のキー「{key}」が重複しています（{line} 行目）。"
                    "ロール名はそれぞれ別の名前にしてください。"
                )
            seen.add(key)
        # 重複が無ければ通常の処理に委ねる
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class RoleEntry:
    """設定ファイル上の「表示名: ロールID」1件分。"""

    name: str
    role_id: int


@dataclass(frozen=True)
class AppConfig:
    """検証済みのアプリケーション設定。"""

    token: str
    guild_id: int
    allowed_user_ids: frozenset[int]
    status_text: str
    presence: discord.Status
    celebrate_channel_id: int | None
    system_log_channel_id: int | None
    bulk_log_channel_id: int | None
    celebrate_message: str
    timezone: ZoneInfo
    ignore_bots: bool
    sync_interval_hours: float
    api_delay_seconds: float
    progress_interval_seconds: float
    join_roles: tuple[RoleEntry, ...]
    grade_roles: tuple[RoleEntry, ...]

    @property
    def join_role_ids(self) -> frozenset[int]:
        """新規加入時に付けるロール ID の集合。"""
        return frozenset(entry.role_id for entry in self.join_roles)

    @property
    def grade_role_ids(self) -> frozenset[int]:
        """学年ロール ID の集合。"""
        return frozenset(entry.role_id for entry in self.grade_roles)


def _parse_id(value: Any, label: str) -> int:
    """Discord の ID（数値または数字文字列）を int に変換する。"""
    # bool は int のサブクラスなので先に除外する
    if isinstance(value, bool):
        raise ConfigError(f"{label} は数値の ID で指定してください: {value!r}")
    # 数値ならそのまま採用する
    if isinstance(value, int):
        parsed = value
    # 引用符付きの数字文字列も許容する
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ConfigError(f"{label} は数値の ID で指定してください: {value!r}")
    # 0 以下は Discord の ID として不正
    if parsed <= 0:
        raise ConfigError(f"{label} に正しい ID を指定してください: {value!r}")
    return parsed


def _parse_number(
    raw: dict[str, Any], key: str, default: float, minimum: float
) -> float:
    """数値設定を読み取り、下限を検証して float で返す。"""
    # 未記入なら既定値を使う
    value = raw.get(key, default)
    # bool や文字列は数値として扱わない
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"「{key}」は数値で指定してください: {value!r}")
    # 下限を下回る値は拒否する
    if value < minimum:
        raise ConfigError(f"「{key}」は {minimum} 以上で指定してください。")
    return float(value)


def _parse_roles(
    raw: dict[str, Any], key: str, required: bool
) -> tuple[RoleEntry, ...]:
    """「表示名: ロールID」のマッピングを RoleEntry のタプルに変換する。"""
    section = raw.get(key)
    # セクション自体が無い（または中身が空）の場合
    if not section:
        # 必須セクションなら設定漏れとして扱う
        if required:
            raise ConfigError(f"「{key}」に 1 つ以上ロールを設定してください。")
        return ()
    # マッピング以外（リスト等）は形式エラー
    if not isinstance(section, dict):
        raise ConfigError(f"「{key}」は「表示名: ロールID」の形式で書いてください。")
    # 記述順を保ったまま変換する（学年ロールは順番が意味を持つ）
    entries = tuple(
        RoleEntry(name=str(name), role_id=_parse_id(value, f"「{key}」の「{name}」"))
        for name, value in section.items()
    )
    # 同じロール ID が複数回書かれていないか確認する
    ids = [entry.role_id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"「{key}」に同じロール ID が重複しています。")
    return entries


def _parse_allowed_users(raw: dict[str, Any]) -> frozenset[int]:
    """コマンドを使えるユーザー ID のホワイトリストを読み取る。"""
    section = raw.get(KEY_ALLOWED_USERS)
    # 1 件だけの場合は単体の値でも書けるようにする
    if isinstance(section, (int, str)) and not isinstance(section, bool):
        section = [section]
    # 未記入や空リストだと誰も操作できなくなるため必須にする
    if not section or not isinstance(section, list):
        raise ConfigError(f"「{KEY_ALLOWED_USERS}」に 1 人以上のユーザー ID を書いてください。")
    return frozenset(_parse_id(value, f"「{KEY_ALLOWED_USERS}」") for value in section)


def _parse_status_text(raw: dict[str, Any]) -> str:
    """カスタムステータスの文言を読み取る（空なら表示しない）。"""
    text = str(raw.get(KEY_STATUS_TEXT) or "").strip()
    # Discord の上限を超えると表示されないため事前に弾く
    if len(text) > MAX_STATUS_LENGTH:
        raise ConfigError(f"「{KEY_STATUS_TEXT}」は {MAX_STATUS_LENGTH} 文字以内にしてください。")
    return text


def _parse_presence(raw: dict[str, Any]) -> discord.Status:
    """オンライン状態を discord.Status に変換する。"""
    name = str(raw.get(KEY_PRESENCE, DEFAULT_PRESENCE)).strip().lower()
    # 対応表に無い値は選択肢付きでエラーにする
    if name not in PRESENCE_MAP:
        raise ConfigError(
            f"「{KEY_PRESENCE}」は {' / '.join(PRESENCE_MAP)} のいずれかで指定してください。"
        )
    return PRESENCE_MAP[name]


def _parse_optional_id(raw: dict[str, Any], key: str) -> int | None:
    """任意の ID 設定を読み取る（未記入・0 なら無効として None）。"""
    value = raw.get(key)
    # 未記入・0・空文字は「使わない」を意味する
    if value in (None, 0, "", "0"):
        return None
    return _parse_id(value, f"「{key}」")


def _parse_log_channel(raw: dict[str, Any], key: str) -> int | None:
    """ログチャンネル ID を読み取る（新キーが無ければ旧キーで代替）。"""
    # 新キーが書かれていればそちらを優先する（0 なら明示的に無効）
    if key in raw:
        return _parse_optional_id(raw, key)
    return _parse_optional_id(raw, KEY_LEGACY_LOG_CHANNEL)


def _parse_celebrate_message(raw: dict[str, Any]) -> str:
    """お祝いメッセージのテンプレートを読み取り、差し込み項目を検証する。"""
    template = str(raw.get(KEY_CELEBRATE_MESSAGE) or DEFAULT_CELEBRATE_MESSAGE)
    try:
        # 未知の {項目} や括弧の閉じ忘れを起動時に検出する
        template.format(**CELEBRATE_SAMPLE)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"「{KEY_CELEBRATE_MESSAGE}」で使えるのは {{name}} {{grade}} {{role}} だけです: {exc}"
        ) from exc
    return template


def _parse_timezone(raw: dict[str, Any]) -> ZoneInfo:
    """タイムゾーン名を ZoneInfo に変換する。"""
    name = str(raw.get(KEY_TIMEZONE, DEFAULT_TIMEZONE))
    try:
        return ZoneInfo(name)
    # 存在しない名前や不正な文字列はまとめて設定エラーにする
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"「{KEY_TIMEZONE}」が不正です: {name}") from exc


def ensure_config_file(path: Path, example_path: Path) -> bool:
    """設定ファイルが無ければ雛形をコピーする。コピーした場合 True。"""
    # 既に存在するなら何もしない
    if path.exists():
        return False
    # 雛形も無ければ作りようがないのでエラーにする
    if not example_path.exists():
        raise ConfigError(f"雛形ファイルが見つかりません: {example_path}")
    shutil.copyfile(example_path, path)
    return True


def load_config(path: Path) -> AppConfig:
    """設定ファイルを読み込み、検証済みの AppConfig を返す。"""
    # ファイルが無ければ案内付きでエラーにする
    if not path.exists():
        raise ConfigError(
            f"設定ファイルが見つかりません: {path}\n"
            "config.example.yaml をコピーして config.yaml を作成してください。"
        )
    try:
        # メモ帳保存の BOM 付き UTF-8 にも対応する
        with path.open(encoding="utf-8-sig") as fp:
            raw = yaml.load(fp, Loader=_UniqueKeyLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML の書式エラーです:\n{exc}") from exc
    # 最上位がマッピングでなければ形式エラー
    if not isinstance(raw, dict):
        raise ConfigError("設定ファイルの形式が正しくありません。")

    # トークンは未記入・プレースホルダのままを弾く
    token = str(raw.get(KEY_TOKEN) or "").strip()
    if not token or token == PLACEHOLDER_TOKEN:
        raise ConfigError(f"「{KEY_TOKEN}」を設定してください。")

    # 各ロールセクションを読み取る（学年ロールは必須）
    join_roles = _parse_roles(raw, KEY_JOIN_ROLES, required=False)
    grade_roles = _parse_roles(raw, KEY_GRADE_ROLES, required=True)
    # 加入ロールと学年ロールが同じだと付け外しが衝突するため禁止する
    overlap = {e.role_id for e in join_roles} & {e.role_id for e in grade_roles}
    if overlap:
        raise ConfigError(
            f"「{KEY_JOIN_ROLES}」と「{KEY_GRADE_ROLES}」に同じロールがあります: "
            + ", ".join(str(role_id) for role_id in sorted(overlap))
        )

    # 真偽値設定は bool 以外を拒否する
    ignore_bots = raw.get(KEY_IGNORE_BOTS, DEFAULT_IGNORE_BOTS)
    if not isinstance(ignore_bots, bool):
        raise ConfigError(f"「{KEY_IGNORE_BOTS}」は true / false で指定してください。")

    return AppConfig(
        token=token,
        guild_id=_parse_id(raw.get(KEY_GUILD_ID), f"「{KEY_GUILD_ID}」"),
        allowed_user_ids=_parse_allowed_users(raw),
        status_text=_parse_status_text(raw),
        presence=_parse_presence(raw),
        celebrate_channel_id=_parse_optional_id(raw, KEY_CELEBRATE_CHANNEL),
        system_log_channel_id=_parse_log_channel(raw, KEY_SYSTEM_LOG_CHANNEL),
        bulk_log_channel_id=_parse_log_channel(raw, KEY_BULK_LOG_CHANNEL),
        celebrate_message=_parse_celebrate_message(raw),
        timezone=_parse_timezone(raw),
        ignore_bots=ignore_bots,
        sync_interval_hours=_parse_number(raw, KEY_SYNC_HOURS, DEFAULT_SYNC_HOURS, 0),
        api_delay_seconds=_parse_number(raw, KEY_API_DELAY, DEFAULT_API_DELAY, 0),
        progress_interval_seconds=_parse_number(
            raw, KEY_PROGRESS_INTERVAL, DEFAULT_PROGRESS_INTERVAL, MIN_PROGRESS_INTERVAL
        ),
        join_roles=join_roles,
        grade_roles=grade_roles,
    )
