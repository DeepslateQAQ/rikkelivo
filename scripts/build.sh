#!/usr/bin/env bash
# 一键构建 rikkelivo 单文件版（Nuitka onefile，本机非容器）。
# 适用于主流发行版（Ubuntu/Debian/Fedora/Arch/macOS）。
# 依赖（自行安装，不在此脚本内集成）：
#   - python3.10+ 及开发头文件（Ubuntu: python3-dev / Debian: python3-dev）
#   - uv（https://docs.astral.sh/uv/）或 pip
#   - Nuitka Linux standalone 必需：patchelf（apt/dnf/brew install patchelf）
#   - C 编译器：gcc 或 clang
# 用法: scripts/build.sh   产物: build/linux/rikkelivo（onefile）
set -euo pipefail

cd "$(dirname "$0")/.."

# 优先用项目 venv（uv 管理，含 nuitka 与已 editable 安装的 rikkelivo）；
# 缺 uv 则退回系统 python3（需自行 pip 装依赖）。
PY="$(command -v python3 || command -v python)"
if [[ -z "$PY" ]]; then
    echo "✗ 未找到 python3" >&2
    exit 1
fi
if command -v uv >/dev/null 2>&1 && [ -f .venv/bin/python ]; then
    PY=".venv/bin/python"
    echo "→ Python: ${PY}（uv venv）"
else
    echo "→ Python: ${PY}（系统，需已装依赖）"
fi

# 产物目录按平台：build/linux（Linux）/ build/macos（macOS）
case "$(uname)" in
  Linux)  PLAT="linux" ;;
  Darwin) PLAT="macos" ;;
  *)      PLAT="$(uname | tr '[:upper:]' '[:lower:]')" ;;
esac
OUT_DIR="build/$PLAT"

# 前置校验：仅提示缺什么，不自动安装
"$PY" -c "import sysconfig, os; assert os.path.exists(os.path.join(sysconfig.get_paths()['include'], 'Python.h')), 'no headers'" 2>/dev/null \
    || { echo "✗ 缺少 Python 开发头文件（如 apt install python3-dev）" >&2; exit 1; }
# patchelf 仅 Linux standalone 必需（macOS 用 install_name_tool，无需 patchelf）
if [ "$PLAT" = "linux" ]; then
    command -v patchelf >/dev/null 2>&1 \
        || { echo "✗ 缺少 patchelf（Nuitka standalone 必需，如 apt install patchelf）" >&2; exit 1; }
fi
"$PY" -c "import nuitka, zstandard" 2>/dev/null \
    || { echo "✗ 缺少 nuitka / zstandard（uv sync --all-groups 或 pip install nuitka zstandard）" >&2; exit 1; }
# 包需可被 Python 导入（src-layout）：uv sync / pip install -e 使 rikkelivo 可解析
"$PY" -c "import rikkelivo" 2>/dev/null \
    || { echo "✗ rikkelivo 不可导入（先 uv sync 或 pip install -e .）" >&2; exit 1; }

echo "→ Nuitka onefile 构建（约 3-5 分钟）..."
"$PY" -m nuitka \
    --assume-yes-for-downloads \
    --output-dir=$OUT_DIR \
    --onefile \
    --no-deployment-flag=self-execution \
    --output-filename=rikkelivo \
    --include-package=rikkelivo \
    --include-package=click \
    --include-package=rich \
    src/rikkelivo/cli.py

echo "✓ 产物: $OUT_DIR/rikkelivo"
