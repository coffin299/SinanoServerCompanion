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
KEY_WELCOME_MESSAGE = "入学祝いメッセージ"
KEY_TIMEZONE = "タイムゾーン"
KEY_IGNORE_BOTS = "Botを対象外にする"
KEY_SYNC_HOURS = "定期同期間隔(時間)"
KEY_API_DELAY = "API操作間隔(秒)"
KEY_PROGRESS_INTERVAL = "進捗更新間隔(秒)"
KEY_JOIN_ROLES = "新規加入時必ず付けるロール"
KEY_GRADE_ROLES = "サーバー参加年数ごとに付けるロール"

# メンション応答（LLM）セクションとその中のキー名
KEY_LLM = "LLM"
KEY_LLM_ENABLED = "有効"
KEY_LLM_API_KEY = "APIキー"
KEY_LLM_BASE_URL = "APIのURL"
KEY_LLM_MODEL = "モデル"
KEY_LLM_PROMPT = "キャラクター設定"
KEY_LLM_TEMPERATURE = "温度"
KEY_LLM_MAX_TOKENS = "最大トークン数"
KEY_LLM_HISTORY = "会話履歴の件数"
KEY_LLM_WEB_SEARCH = "Web検索"
KEY_LLM_SEARCH_RESULTS = "検索結果の件数"
KEY_LLM_CHANNELS = "応答するチャンネルID"
KEY_LLM_COOLDOWN = "ユーザーごとの間隔(秒)"
KEY_LLM_TIMEOUT = "タイムアウト(秒)"
KEY_LLM_WAITING = "待機中メッセージ"

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

# メンション応答（LLM）の既定値（NVIDIA NIM の OpenAI 互換エンドポイントを想定）
DEFAULT_LLM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_LLM_MODEL = "meta/llama-3.3-70b-instruct"
DEFAULT_LLM_PROMPT = (
    "あなたは Discord サーバーにいるフレンドリーなアシスタント Bot です。"
    "日本語で、簡潔かつ親しみやすく答えてください。Discord のマークダウンが使えます。"
)
DEFAULT_LLM_TEMPERATURE = 0.7
MAX_LLM_TEMPERATURE = 2.0
DEFAULT_LLM_MAX_TOKENS = 1024
DEFAULT_LLM_HISTORY = 10
DEFAULT_LLM_SEARCH_RESULTS = 5
MAX_LLM_SEARCH_RESULTS = 10
DEFAULT_LLM_COOLDOWN = 3.0
DEFAULT_LLM_TIMEOUT = 60.0
MIN_LLM_TIMEOUT = 5.0
DEFAULT_LLM_WAITING = "💭 考え中…"

# 「オンライン状態」に書ける値と discord.Status の対応
PRESENCE_MAP = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

# カスタムステータスの最大文字数（Discord の上限）
MAX_STATUS_LENGTH = 128
# 1 メッセージの最大文字数（Discord の上限）
MAX_MESSAGE_LENGTH = 2000

# お祝いメッセージの既定文と、差し込める項目の検証用サンプル
DEFAULT_CELEBRATE_MESSAGE = "🎉 祝！{name}さんが{grade}年生になりました！"
CELEBRATE_SAMPLE = {"name": "名無し", "grade": 2, "role": "二年生"}
DEFAULT_WELCOME_MESSAGE = "🌸 祝！{name}さんが{server}に入学しました！ようこそ！"
WELCOME_SAMPLE = {"name": "名無し", "server": "サーバー", "role": "一年生"}


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
class LLMConfig:
    """メンション応答（LLM）の設定。"""

    enabled: bool
    api_key: str
    base_url: str
    # 上から順に試すモデル（先頭が失敗したら次へフォールバック）
    models: tuple[str, ...]
    system_prompt: str
    temperature: float
    max_tokens: int
    history_limit: int
    web_search: bool
    search_results: int
    channel_ids: frozenset[int]
    cooldown_seconds: float
    timeout_seconds: float
    # 応答待ちの間に表示する文言（空なら表示しない）
    waiting_message: str


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
    welcome_message: str
    timezone: ZoneInfo
    ignore_bots: bool
    sync_interval_hours: float
    api_delay_seconds: float
    progress_interval_seconds: float
    join_roles: tuple[RoleEntry, ...]
    grade_roles: tuple[RoleEntry, ...]
    llm: LLMConfig

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
    raw: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float | None = None,
) -> float:
    """数値設定を読み取り、上下限を検証して float で返す。"""
    # 未記入なら既定値を使う
    value = raw.get(key, default)
    # bool や文字列は数値として扱わない
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"「{key}」は数値で指定してください: {value!r}")
    # 下限を下回る値は拒否する
    if value < minimum:
        raise ConfigError(f"「{key}」は {minimum} 以上で指定してください。")
    # 上限が決まっている設定は超過も拒否する
    if maximum is not None and value > maximum:
        raise ConfigError(f"「{key}」は {maximum} 以下で指定してください。")
    return float(value)


def _parse_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """整数設定を読み取り、上下限を検証して int で返す。"""
    value = _parse_number(raw, key, default, minimum, maximum)
    # 件数などに小数が書かれていたら拒否する
    if not value.is_integer():
        raise ConfigError(f"「{key}」は整数で指定してください。")
    return int(value)


def _parse_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    """真偽値設定を読み取る（true / false 以外は拒否）。"""
    value = raw.get(key, default)
    # "yes" や 1 などの曖昧な値は誤設定とみなす
    if not isinstance(value, bool):
        raise ConfigError(f"「{key}」は true / false で指定してください。")
    return value


def _parse_id_list(raw: dict[str, Any], key: str) -> frozenset[int]:
    """ID のリスト設定を読み取る（1 件だけなら単体の値でも可、未記入なら空）。"""
    section = raw.get(key)
    # 未記入や空リストは空集合として扱う
    if section in (None, "", []):
        return frozenset()
    # 1 件だけの場合は単体の値でも書けるようにする
    if isinstance(section, (int, str)) and not isinstance(section, bool):
        section = [section]
    # リスト以外（マッピング等）は形式エラー
    if not isinstance(section, list):
        raise ConfigError(f"「{key}」は ID のリストで書いてください。")
    return frozenset(_parse_id(value, f"「{key}」") for value in section)


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
    user_ids = _parse_id_list(raw, KEY_ALLOWED_USERS)
    # 未記入や空リストだと誰も操作できなくなるため必須にする
    if not user_ids:
        raise ConfigError(f"「{KEY_ALLOWED_USERS}」に 1 人以上のユーザー ID を書いてください。")
    return user_ids


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


def _parse_template(
    raw: dict[str, Any], key: str, default: str, sample: dict[str, Any]
) -> str:
    """メッセージテンプレートを読み取り、差し込み項目を検証する。

    キーが無ければ既定文、空文字なら「送らない」として空文字を返す。
    """
    # キー自体が無ければ既定文を使う
    if key not in raw:
        return default
    template = str(raw.get(key) or "").strip()
    # 空なら通知を無効にする
    if not template:
        return ""
    try:
        # 未知の {項目} や括弧の閉じ忘れを起動時に検出する
        template.format(**sample)
    except (KeyError, IndexError, ValueError) as exc:
        allowed = " ".join(f"{{{name}}}" for name in sample)
        raise ConfigError(f"「{key}」で使えるのは {allowed} だけです: {exc}") from exc
    return template


def _parse_timezone(raw: dict[str, Any]) -> ZoneInfo:
    """タイムゾーン名を ZoneInfo に変換する。"""
    name = str(raw.get(KEY_TIMEZONE, DEFAULT_TIMEZONE))
    try:
        return ZoneInfo(name)
    # 存在しない名前や不正な文字列はまとめて設定エラーにする
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"「{KEY_TIMEZONE}」が不正です: {name}") from exc


def _parse_text(raw: dict[str, Any], key: str, default: str) -> str:
    """文字列設定を読み取る（未記入・空なら既定値）。"""
    return str(raw.get(key) or "").strip() or default


def _parse_optional_text(raw: dict[str, Any], key: str, default: str) -> str:
    """文字列設定を読み取る（キーが無ければ既定値、空なら無効として空文字）。"""
    # キー自体が無ければ既定値を使う
    if key not in raw:
        return default
    text = str(raw.get(key) or "").strip()
    # Discord の 1 メッセージの上限を超えると送れないため事前に弾く
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ConfigError(f"「{key}」は {MAX_MESSAGE_LENGTH} 文字以内にしてください。")
    return text


def _parse_text_list(raw: dict[str, Any], key: str, default: str) -> tuple[str, ...]:
    """文字列 1 つ、または文字列のリストを読み取る（未記入・空なら既定値のみ）。"""
    value = raw.get(key)
    # 単体の文字列は 1 件のリストとして扱う
    if isinstance(value, str):
        value = [value]
    # 未記入はリストが空のときと同じく既定値にする
    if value is None:
        value = []
    # リスト以外（マッピング等）は形式エラー
    if not isinstance(value, list):
        raise ConfigError(f"「{key}」は文字列、または文字列のリストで書いてください。")
    # 空要素を除き、重複は最初の 1 つだけ残して順番を保つ
    items = tuple(dict.fromkeys(str(item).strip() for item in value if str(item or "").strip()))
    return items or (default,)


def _parse_llm(raw: dict[str, Any]) -> LLMConfig:
    """メンション応答（LLM）セクションを読み取る。"""
    section = raw.get(KEY_LLM) or {}
    # セクションはマッピングで書く必要がある
    if not isinstance(section, dict):
        raise ConfigError(f"「{KEY_LLM}」は「キー: 値」の形式で書いてください。")
    enabled = _parse_bool(section, KEY_LLM_ENABLED, False)
    api_key = str(section.get(KEY_LLM_API_KEY) or "").strip()
    # 有効なのに API キーが無いと応答できないため起動時に弾く
    if enabled and not api_key:
        raise ConfigError(f"「{KEY_LLM}」の「{KEY_LLM_API_KEY}」を設定してください。")
    return LLMConfig(
        enabled=enabled,
        api_key=api_key,
        base_url=_parse_text(section, KEY_LLM_BASE_URL, DEFAULT_LLM_BASE_URL),
        models=_parse_text_list(section, KEY_LLM_MODEL, DEFAULT_LLM_MODEL),
        system_prompt=_parse_text(section, KEY_LLM_PROMPT, DEFAULT_LLM_PROMPT),
        temperature=_parse_number(
            section, KEY_LLM_TEMPERATURE, DEFAULT_LLM_TEMPERATURE, 0, MAX_LLM_TEMPERATURE
        ),
        max_tokens=_parse_int(section, KEY_LLM_MAX_TOKENS, DEFAULT_LLM_MAX_TOKENS, 1),
        history_limit=_parse_int(section, KEY_LLM_HISTORY, DEFAULT_LLM_HISTORY, 0),
        web_search=_parse_bool(section, KEY_LLM_WEB_SEARCH, True),
        search_results=_parse_int(
            section, KEY_LLM_SEARCH_RESULTS, DEFAULT_LLM_SEARCH_RESULTS, 1, MAX_LLM_SEARCH_RESULTS
        ),
        channel_ids=_parse_id_list(section, KEY_LLM_CHANNELS),
        cooldown_seconds=_parse_number(section, KEY_LLM_COOLDOWN, DEFAULT_LLM_COOLDOWN, 0),
        timeout_seconds=_parse_number(
            section, KEY_LLM_TIMEOUT, DEFAULT_LLM_TIMEOUT, MIN_LLM_TIMEOUT
        ),
        waiting_message=_parse_optional_text(section, KEY_LLM_WAITING, DEFAULT_LLM_WAITING),
    )


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

    return AppConfig(
        token=token,
        guild_id=_parse_id(raw.get(KEY_GUILD_ID), f"「{KEY_GUILD_ID}」"),
        allowed_user_ids=_parse_allowed_users(raw),
        status_text=_parse_status_text(raw),
        presence=_parse_presence(raw),
        celebrate_channel_id=_parse_optional_id(raw, KEY_CELEBRATE_CHANNEL),
        system_log_channel_id=_parse_log_channel(raw, KEY_SYSTEM_LOG_CHANNEL),
        bulk_log_channel_id=_parse_log_channel(raw, KEY_BULK_LOG_CHANNEL),
        celebrate_message=_parse_template(
            raw, KEY_CELEBRATE_MESSAGE, DEFAULT_CELEBRATE_MESSAGE, CELEBRATE_SAMPLE
        ),
        welcome_message=_parse_template(
            raw, KEY_WELCOME_MESSAGE, DEFAULT_WELCOME_MESSAGE, WELCOME_SAMPLE
        ),
        timezone=_parse_timezone(raw),
        ignore_bots=_parse_bool(raw, KEY_IGNORE_BOTS, DEFAULT_IGNORE_BOTS),
        sync_interval_hours=_parse_number(raw, KEY_SYNC_HOURS, DEFAULT_SYNC_HOURS, 0),
        api_delay_seconds=_parse_number(raw, KEY_API_DELAY, DEFAULT_API_DELAY, 0),
        progress_interval_seconds=_parse_number(
            raw, KEY_PROGRESS_INTERVAL, DEFAULT_PROGRESS_INTERVAL, MIN_PROGRESS_INTERVAL
        ),
        join_roles=join_roles,
        grade_roles=grade_roles,
        llm=_parse_llm(raw),
    )
