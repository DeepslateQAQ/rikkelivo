#!/usr/bin/env bash
# 在 Ubuntu Docker 容器里用 Nuitka 构建单文件 rikkelivo。
# 适用场景：NixOS 等系统缺少常规发行版的动态库布局（libz 等），
# 或需要产出在常见发行版 glibc 下构建的二进制。
# 基线用 ubuntu:22.04（glibc 2.35）：产物要求目标系统 glibc >= 2.34，
# 可覆盖 Ubuntu 22.04+ / Debian 12+ / RHEL 9+ 等；换旧镜像可扩大兼容面。
#
# 用法:
#   scripts/build-in-container.sh [--cleanup]
#   产物 dist/rikkelivo（~17M，构建约 5 分钟）
set -euo pipefail
cd "$(dirname "$0")/.."

CLEANUP=0
for arg in "$@"; do
  [ "$arg" = "--cleanup" ] && CLEANUP=1
done

IMAGE="ubuntu:22.04"
NAME="rikkelivo-builder"
WORK="/work"
JOBS="$(nproc)"

if [ "$CLEANUP" = "1" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
fi

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE"

# 复用已有容器（避免每次重装 apt/uv）；镜像不匹配或 uv 缺失才重建
if ! docker exec "$NAME" bash -lc 'command -v uv' >/dev/null 2>&1 || [ "$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null)" != "$IMAGE" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" -v "$(pwd):$WORK" "$IMAGE" sleep infinity >/dev/null
  docker exec "$NAME" bash -lc 'apt-get update -qq >/dev/null && apt-get install -y -qq curl gcc patchelf ccache >/dev/null 2>&1 && curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1'
fi

# uv 托管 Python（3.13）满足 requires-python，且比系统 python3.10 新
UV='export PATH=/root/.local/bin:$PATH; cd /work && mkdir -p dist && uv run --frozen'

docker exec "$NAME" bash -lc "$UV --group build nuitka --onefile --assume-yes-for-downloads --no-deployment-flag=self-execution --output-filename=/work/dist/rikkelivo --remove-output --jobs=$JOBS --include-package=rikkelivo --include-package=click --include-package=rich --include-package=questionary --include-package=prompt_toolkit --include-package=wcwidth --include-package-data=rikkelivo src/rikkelivo/cli.py"
echo "构建完成: $(pwd)/dist/rikkelivo"
