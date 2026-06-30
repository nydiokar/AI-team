param(
  [switch]$RunLive,
  [string]$Repo = "",
  [string]$OutDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $OutDir) {
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $OutDir = Join-Path (Get-Location) ".claude-p0-live-$stamp"
}
if (-not $Repo) {
  $Repo = Join-Path $OutDir "safe-repo"
}

$turns = @(
  "say exactly: turn one",
  "say exactly: turn two",
  "say exactly: turn three"
)

function Write-Step($Text) {
  Write-Host "[claude-p0] $Text"
}

function Assert-LiveAllowed {
  if (-not $RunLive) {
    Write-Step "dry run only. Re-run with -RunLive and AI_TEAM_ALLOW_LIVE_CLAUDE_P0=1 to execute."
    exit 0
  }
  if ($env:AI_TEAM_ALLOW_LIVE_CLAUDE_P0 -ne "1") {
    throw "Refusing live Claude calls. Set AI_TEAM_ALLOW_LIVE_CLAUDE_P0=1 and pass -RunLive."
  }
}

function Invoke-PrintMatrix($Name, [bool]$IncludePartial) {
  $sessionId = ""
  for ($i = 0; $i -lt $turns.Count; $i++) {
    $turn = $i + 1
    $stdout = Join-Path $OutDir "$Name-turn$turn.ndjson"
    $stderr = Join-Path $OutDir "$Name-turn$turn.stderr.txt"
    $args = @("--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions")
    if ($IncludePartial) { $args += "--include-partial-messages" }
    if ($sessionId) { $args = @("--resume", $sessionId) + $args } else { $args += @("--session-id", [guid]::NewGuid().ToString()) }
    $args += @("-p")
    Write-Step "$Name turn $turn"
    $turns[$i] | & claude @args 1> $stdout 2> $stderr
    $line = Get-Content $stdout | Where-Object { $_ -match '"session_id"' } | Select-Object -Last 1
    if ($line) {
      $json = $line | ConvertFrom-Json
      if ($json.session_id) { $sessionId = [string]$json.session_id }
    }
  }
}

function Invoke-SdkMatrix {
  $sdkScript = Join-Path $OutDir "sdk_probe.py"
  @"
import asyncio
import json
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, AssistantMessage, TextBlock

TURNS = ["say exactly: turn one", "say exactly: turn two", "say exactly: turn three"]

async def main() -> None:
    client = ClaudeSDKClient(options=ClaudeAgentOptions(permission_mode="bypassPermissions"))
    await client.connect()
    try:
        for index, prompt in enumerate(TURNS, start=1):
            await client.query(prompt)
            async for msg in client.receive_response():
                row = {"turn": index, "message_type": type(msg).__name__}
                usage = getattr(msg, "usage", None)
                if usage:
                    row["usage"] = usage
                sid = getattr(msg, "session_id", "")
                if sid:
                    row["session_id"] = sid
                if isinstance(msg, AssistantMessage):
                    row["text"] = "".join(block.text for block in msg.content if isinstance(block, TextBlock))
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await client.disconnect()

asyncio.run(main())
"@ | Set-Content -Path $sdkScript -Encoding UTF8
  Write-Step "sdk turn matrix"
  python $sdkScript 1> (Join-Path $OutDir "sdk.ndjson") 2> (Join-Path $OutDir "sdk.stderr.txt")
}

Write-Step "output: $OutDir"
Write-Step "repo: $Repo"
Write-Step "matrix: print_with_partial, print_without_partial, sdk"
Assert-LiveAllowed
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path $Repo | Out-Null
Push-Location $Repo
try {
  if (-not (Test-Path ".git")) { git init | Out-Null }
  Invoke-PrintMatrix "print_with_partial" $true
  Invoke-PrintMatrix "print_without_partial" $false
  Invoke-SdkMatrix
  Write-Step "done. Inspect NDJSON usage rows under $OutDir"
}
finally {
  Pop-Location
}
