# 一键构建 rikkelivo Windows 单文件版（Nuitka onefile）。
# 需本机已安装 Windows 版 Python 3.12 + uv（或 pip）。用法: .\scripts\build.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = $null
# 优先项目 venv（uv sync 后存在，含 nuitka 与 editable 安装的 rikkelivo）
if (Test-Path .venv\Scripts\python.exe) {
    $py = ".venv\Scripts\python.exe"
} else {
    foreach ($cand in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "python.exe"
    )) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
    }
}
if (-not $py) {
    Write-Error "未找到 Windows 版 Python，请先安装 Python 3.12"
    exit 1
}

Write-Host "[INFO] Python: $py"

# 包需可被 Python 导入（src-layout）：uv sync 或 pip install -e 使 rikkelivo 可解析
# setup-uv 提供 PATH 上的 uv.exe（非 venv 内可 `python -m uv` 导入），故优先 Get-Command uv
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uvCmd) {
    # uv 会把进度写 stderr：在 Stop 下会被当成 NativeCommandError 打断，故局部静默 + 显式检查退出码
    $oldEAP = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
    & uv sync --all-groups 2>$null
    $uvExit = $LASTEXITCODE; $ErrorActionPreference = $oldEAP
    if ($uvExit -ne 0) { exit 1 }
} else {
    $oldEAP = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
    & $py -m uv --version *> $null
    $uvOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $oldEAP
    if ($uvOk) {
        $oldEAP = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
        & $py -m uv sync --all-groups 2>$null
        $uvExit = $LASTEXITCODE; $ErrorActionPreference = $oldEAP
        if ($uvExit -ne 0) { exit 1 }
    } else {
        & $py -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) { Write-Error "未找到 uv 或 pip"; exit 1 }
        & $py -m pip install -q -e . nuitka zstandard
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
}
# uv sync / pip install -e 会在仓库根创建 .venv（含 nuitka 与 editable 的 rikkelivo）：
# 若此前用的是系统 python，sync 后应改用 .venv 的 python，避免后续 import/nuitka 走旧解释器
if (Test-Path .venv\Scripts\python.exe) {
    $py = ".venv\Scripts\python.exe"
    Write-Host "[INFO] 切换到项目 venv: $py"
}
& $py -c "import rikkelivo; print('IMPORT_OK')"
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "[INFO] Nuitka onefile 构建中（约 3-5 分钟）..."
$nuitkaArgs = @(
    "--mingw64",
    "--assume-yes-for-downloads",
    "--output-dir=build\win",
    "--onefile",
    "--no-deployment-flag=self-execution",
    "--output-filename=rikkelivo.exe",
    "--include-package=rikkelivo",
    "--include-package=click",
    "--include-package=rich",
    "src\rikkelivo\cli.py"
)
& $py -m nuitka $nuitkaArgs
if ($LASTEXITCODE -ne 0) { Write-Error "Nuitka 构建失败"; exit 1 }

Write-Host "[OK] 产物: build\win\rikkelivo.exe"
