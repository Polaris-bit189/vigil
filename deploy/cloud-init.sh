#!/usr/bin/env bash
# ECS 首次启动时的初始化（cloud-init user_data）
#
# 为什么需要它：auto-deploy 会把代码传上去、把服务拉起来，但它**准备的是
# 一个"能跑 Node/Python/Java"的通用环境**。VIGIL 云端还需要三样它不管的东西：
#   ① RDS/Redis 的客户端（pymysql / redis 是可选依赖组 `cloud`）
#   ② `/etc/vigil/vigil.env`（含 DSN，权限 600）
#   ③ systemd 单元
#
# ⚠️ 这个脚本**不含任何密码**。DSN 由部署脚本在**部署时**写入
#    `/etc/vigil/vigil.env`（见 deploy/deploy.sh），而不是烤进镜像 ——
#    烤进去的话它就留在了镜像与 cloud-init 日志里。

set -euo pipefail

APP_USER=vigil
APP_DIR=/opt/vigil
ENV_DIR=/etc/vigil

echo "[cloud-init] 建用户与目录"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

# ⚠️ `EnvironmentFile` 指向的文件必须**先存在**，否则 systemd 启动会报
#    "Failed to load environment files"。先建一个占位（权限 600、内容为空），
#    真正的 DSN 由部署脚本覆盖写入。
touch "$ENV_DIR/vigil.env"
chown root:root "$ENV_DIR/vigil.env"
chmod 600 "$ENV_DIR/vigil.env"

echo "[cloud-init] 装 Python"
# 用系统自带的 python3（EulerOS/Ubuntu 都有）。不装 uv ——
# 部署脚本会用 `python3 -m venv`，那是最少依赖的路径（评委复现友好）。
if ! command -v python3 >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3 python3-pip python3-venv
    fi
fi

echo "[cloud-init] 准备 venv 目录"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "[cloud-init] 完成 —— 代码与 systemd 单元由部署脚本安装"
