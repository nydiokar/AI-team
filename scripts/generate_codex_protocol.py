"""Regenerate the adapter's protocol subset from its pinned Codex binary."""
import json
import subprocess
import tempfile
from pathlib import Path

VERSION = "codex-cli 0.153.2"
REQUESTS = ("initialize", "thread/start", "thread/resume", "thread/unsubscribe",
            "thread/compact/start", "turn/start", "turn/interrupt")
RESPONSES = ("ThreadStartResponse", "ThreadResumeResponse", "ThreadUnsubscribeResponse",
             "ThreadCompactStartResponse", "TurnStartResponse", "TurnInterruptResponse")


def generate() -> None:
    version: str = subprocess.check_output(["codex", "--version"], text=True, timeout=10).strip()
    if version != VERSION:
        raise RuntimeError(f"Expected {VERSION}, got {version}")
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as directory:
        subprocess.run(["codex", "app-server", "generate-json-schema", "--out", directory],
                       check=True, timeout=30)
        root: Path = Path(directory)
        schemas: dict = {}
        request: dict = json.loads((root / "ClientRequest.json").read_text())
        for variant in request["oneOf"]:
            method: str = variant["properties"]["method"]["enum"][0]
            if method in REQUESTS:
                schemas[method] = {**variant, "definitions": request["definitions"]}
        schemas["notifications"] = json.loads((root / "ServerNotification.json").read_text())
        base: dict = json.loads((root / "codex_app_server_protocol.schemas.json").read_text())
        schemas["InitializeResponse"] = json.loads(json.dumps(
            base["definitions"]["InitializeResponse"]).replace("#/definitions/v2/", "#/definitions/"))
        schemas["InitializeResponse"]["definitions"] = json.loads(
            (root / "codex_app_server_protocol.v2.schemas.json").read_text())["definitions"]
        for name in RESPONSES:
            schemas[name] = json.loads((root / "v2" / f"{name}.json").read_text())
        # Retain only transitively referenced definitions, without modifying schemas.
        for schema in schemas.values():
            definitions: dict = schema.pop("definitions", {})
            needed: dict = {}
            def visit(value: object) -> None:
                if isinstance(value, dict):
                    ref = value.get("$ref", "")
                    if isinstance(ref, str) and ref.startswith("#/definitions/"):
                        name = ref.removeprefix("#/definitions/")
                        if name not in needed:
                            needed[name] = definitions[name]
                            visit(definitions[name])
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(schema)
            if needed:
                schema["definitions"] = needed
        target: Path = Path(__file__).resolve().parents[1] / "src/backends/codex_protocol.json"
        target.write_text(json.dumps({"version": VERSION, "schemas": schemas},
                                    sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    generate()
