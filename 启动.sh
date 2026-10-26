#!/bin/bash
# llama.cpp 服务管理器 —— 启动脚本
cd "$(dirname "$0")" || exit 1

# MUSA 运行时库（libmusa.so.4 内部 dlopen libdrm_mtgpu 依赖此变量）
BIN="$HOME/deepseek_workspace/deepseek_harness/llama.cpp/build-musa/bin"
export LD_LIBRARY_PATH="/usr/local/musa/lib:$BIN${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec python3 gui_server.py
