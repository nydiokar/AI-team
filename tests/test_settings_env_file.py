import os
import subprocess
import sys
from pathlib import Path


SCRIPT = """
import os
from config import config
assert config.telegram.bot_token == ""
assert os.getenv("GATEWAY_TELEGRAM_BOT_TOKEN") is None
assert os.getenv("GATEWAY_TELEGRAM_ALLOWED_USERS") is None
assert os.getenv("GATEWAY_TELEGRAM_CHAT_ID") is None
"""


SCRIPT_OVERRIDE = """
from config import config
assert config.telegram.bot_token == "file-token"
assert config.telegram.allowed_users == [123]
assert config.telegram.notification_chat_id == 456
"""


def _run_settings_import(script: str, env_file: Path) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] = os.environ.copy()
    env.update(
        {
            "AI_TEAM_ENV_FILE": str(env_file),
            "GATEWAY_TELEGRAM_BOT_TOKEN": "stale-token",
            "GATEWAY_TELEGRAM_ALLOWED_USERS": "999",
            "GATEWAY_TELEGRAM_CHAT_ID": "999",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_explicit_env_file_clears_commented_managed_env_keys(tmp_path: Path) -> None:
    env_file: Path = tmp_path / "gateway.env"
    env_file.write_text(
        "# GATEWAY_TELEGRAM_BOT_TOKEN=file-token\n"
        "# GATEWAY_TELEGRAM_ALLOWED_USERS=123\n"
        "# GATEWAY_TELEGRAM_CHAT_ID=456\n"
        "MESH_ENABLED=false\n",
        encoding="utf-8",
    )

    result: subprocess.CompletedProcess[str] = _run_settings_import(SCRIPT, env_file)

    assert result.returncode == 0, result.stderr


def test_explicit_env_file_overrides_stale_managed_env_values(tmp_path: Path) -> None:
    env_file: Path = tmp_path / "gateway.env"
    env_file.write_text(
        "GATEWAY_TELEGRAM_BOT_TOKEN=file-token\n"
        "GATEWAY_TELEGRAM_ALLOWED_USERS=123\n"
        "GATEWAY_TELEGRAM_CHAT_ID=456\n"
        "MESH_ENABLED=false\n",
        encoding="utf-8",
    )

    result: subprocess.CompletedProcess[str] = _run_settings_import(SCRIPT_OVERRIDE, env_file)

    assert result.returncode == 0, result.stderr