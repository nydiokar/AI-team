"""Credential-free Codex control smoke for the isolated worker image.

Creates an empty native thread and closes the app server. Never starts a turn.
Run only in a throwaway container with --network none and no host home mounts.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from src.backends.codex_app_server import CodexAppServerClient


def main() -> None:
    executable = shutil.which("codex")
    assert executable is not None, "worker image must include its pinned Codex CLI"
    with tempfile.TemporaryDirectory(prefix="codex-native-offline-") as root:
        home = Path(root)
        (home / "codex").mkdir()
        client = CodexAppServerClient(executable, {
            "HOME": root, "CODEX_HOME": str(home / "codex"),
            "PATH": os.defpath + ":/usr/local/bin", "OTEL_SDK_DISABLED": "true",
        })
        process = None
        try:
            client.start()
            process = client.process
            assert process is not None and process.poll() is None
            response = client.attach_thread("", root, None, {})
            thread = response.get("thread")
            assert isinstance(thread, dict) and isinstance(thread.get("id"), str), response
            assert thread["id"]
        except Exception as exc:
            raise AssertionError(f"native app-server failed: {bytes(client.stderr).decode(errors='replace')}") from exc
        finally:
            client.close()
        assert process is not None and process.poll() is not None
        assert all(not reader.is_alive() for reader in client.readers)
        print(json.dumps({"codex_initialize": "passed", "empty_thread": "passed", "cleanup": "passed", "model_turns": 0}))


if __name__ == "__main__":
    main()
