"""SinanoServerCompanion の起動エントリポイント。"""

import logging
import sys
from pathlib import Path

# 自作モジュールの __pycache__ を作らないよう import 前に無効化する
sys.dont_write_bytecode = True

import discord  # noqa: E402

from companion.bot import CompanionBot  # noqa: E402
from companion.config import (  # noqa: E402
    ConfigError,
    ensure_config_file,
    load_config,
)

# 設定ファイルと雛形は main.py と同じフォルダに置く
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
EXAMPLE_PATH = BASE_DIR / "config.example.yaml"

# 初回に設定ファイルを生成して終了したことを start.bat へ伝える終了コード
EXIT_CONFIG_CREATED = 2

log = logging.getLogger("companion")


def main() -> int:
    """設定を読み込んで Bot を起動する。戻り値は終了コード。"""
    discord.utils.setup_logging(level=logging.INFO)
    try:
        # 初回起動時は雛形から config.yaml を作り、編集を促して終了する
        if ensure_config_file(CONFIG_PATH, EXAMPLE_PATH):
            log.info(
                "config.yaml を作成しました。トークン・サーバー ID・ロール ID などを"
                "記入してから再度起動してください: %s",
                CONFIG_PATH,
            )
            return EXIT_CONFIG_CREATED
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        log.error("設定エラー: %s", exc)
        return 1
    bot = CompanionBot(config, CONFIG_PATH)
    try:
        bot.run(config.token, log_handler=None)
    except discord.LoginFailure:
        log.error("ログインに失敗しました。Bot トークンを確認してください。")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Developer Portal で Bot の「SERVER MEMBERS INTENT」を有効にしてください。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
