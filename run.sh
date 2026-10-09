#!/bin/bash
# 本地 AI Token 账本 — macOS/Linux 启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==========================================="
echo "  本地 AI Token 账本"
echo "==========================================="
echo ""

# 检测 Python
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo ""
    echo "错误：未找到 Python。请确保 Python 3 已安装。"
    echo "macOS: brew install python3"
    echo "或从 https://www.python.org/downloads/ 下载"
    exit 1
fi

echo "[1/2] 正在同步数据..."
$PYTHON "$SCRIPT_DIR/sync_tokens.py"
if [ $? -ne 0 ]; then
    echo ""
    echo "同步失败！"
    exit 1
fi

echo ""
echo "[2/2] 正在打开仪表盘..."

# 根据不同操作系统打开浏览器
if [[ "$OSTYPE" == "darwin"* ]]; then
    open "$SCRIPT_DIR/dashboard.html"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if command -v xdg-open &>/dev/null; then
        xdg-open "$SCRIPT_DIR/dashboard.html"
    else
        echo "请手动打开: $SCRIPT_DIR/dashboard.html"
    fi
else
    echo "请手动打开: $SCRIPT_DIR/dashboard.html"
fi

echo ""
echo "仪表盘已在浏览器中打开。"
echo "每次运行 run.sh 都会重新同步数据。"
