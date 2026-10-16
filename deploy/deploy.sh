#!/usr/bin/env bash
# VIGIL 云端部署 —— 一步到位
#
#     ./deploy/deploy.sh              # 完整部署（建资源 + 传代码 + 起服务 + 验收）
#     ./deploy/deploy.sh --plan-only  # 只看计划，不动资源
#     ./deploy/deploy.sh --destroy    # 销毁全部资源（**跑完必须做**）
#
# ── 前置（三项，卡在账号操作上，只能你自己做）──────────────────
#
#  ① AK/SK 只走环境变量（华为云控制台 → 我的凭证 → 访问密钥）
#       export HW_ACCESS_KEY="..."
#       export HW_SECRET_KEY="..."
#
#  ② 密码也走 TF_VAR_（绝不写进 terraform.tfvars）
#       export TF_VAR_rds_password="$(openssl rand -base64 24)"
#       export TF_VAR_dcs_password="$(openssl rand -base64 24)"
#
#  ③ Terraform ≥ 1.8
#       terraform -version
#
# ── 为什么要这个脚本而不是直接用 auto-deploy ──────────────────
#
#  官方 auto-deploy Skill 的 SKILL.md 明写：
#      Out of scope: RDS/Redis/OBS/MQ/ELB/CCE/Kubernetes stacks
#  而赛题任务第 2 条要求「关联 RDS、REDIS 等华为云服务的快速开通创建」。
#
#  本脚本把两件事拼起来：
#    · `deploy/terraform/`（我们从 Skill 的模板 fork 出来 + 加了 rds.tf/dcs.tf）
#      —— Terraform 根是**我们自己的**，所以 `destroy` 一次性清干净
#    · auto-deploy 只负责「传代码 + 起服务 + 健康检查」那两段
#
#  ⚠️ 为什么不用 Skill 的 `.auto-deploy/terraform/`：那是它的工作目录，
#     重跑会覆盖我们的 rds.tf/dcs.tf。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/deploy/terraform"
APP_NAME="${APP_NAME:-vigil-demo}"
REGION="${REGION:-cn-north-4}"

PLAN_ONLY=0
DESTROY=0
for arg in "$@"; do
  case "$arg" in
    --plan-only) PLAN_ONLY=1 ;;
    --destroy)   DESTROY=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[部署失败]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 前置检查（失败就说清缺什么、怎么补）──────────────────────
need() {
  command -v "$1" >/dev/null 2>&1 || die "缺少 $1。$2"
}
need terraform "装它：https://developer.hashicorp.com/terraform/install（需 ≥1.8）"
need bash "需要 Git Bash / WSL"

TF_VER="$(terraform version -json 2>/dev/null | grep -o '"terraform_version":"[^"]*"' | cut -d'"' -f4 || true)"
if [ -n "$TF_VER" ]; then
  log "Terraform $TF_VER"
fi

if [ -z "${HW_ACCESS_KEY:-}" ] && [ -z "${HUAWEICLOUD_ACCESS_KEY:-}" ]; then
  die "没有 AK。export HW_ACCESS_KEY=... （或 HUAWEICLOUD_ACCESS_KEY）
  ⚠️ 密钥**只走环境变量**，绝不写进任何文件。"
fi
if [ -z "${TF_VAR_rds_password:-}" ] || [ -z "${TF_VAR_dcs_password:-}" ]; then
  die "没有数据库密码。先设：
    export TF_VAR_rds_password=\"\$(openssl rand -base64 24)\"
    export TF_VAR_dcs_password=\"\$(openssl rand -base64 24)\""
fi

# ── 销毁路径（**跑完必须做**：ECS/EIP/RDS 按小时计费）──────────
if [ "$DESTROY" -eq 1 ]; then
  log "销毁资源（RDS / DCS / ECS / EIP / 安全组）"
  cd "$TF_DIR"
  terraform destroy -auto-approve
  log "✅ 已销毁。到控制台复核一遍：ECS / EIP / RDS / DCS 都应消失"
  exit 0
fi

# ── 1. Terraform ────────────────────────────────────────────────
cd "$TF_DIR"
log "terraform init（provider 走华为云镜像源）"
terraform init -input=false

if [ "$PLAN_ONLY" -eq 1 ]; then
  log "只出计划，不 apply"
  terraform plan -var "app_name=$APP_NAME" -var "region=$REGION"
  exit 0
fi

log "terraform apply —— 建 ECS/EIP/SG/VPC + RDS + DCS"
# ⚠️ RDS/DCS 实例创建要几分钟到十几分钟，这是**正常**的（不是卡死）
terraform apply -auto-approve -var "app_name=$APP_NAME" -var "region=$REGION"

# ⚠️ 只取地址与端口 —— 密码不从这里走（见下）
RDS_ADDR="$(terraform output -raw rds_address)"
DCS_ADDR="$(terraform output -raw dcs_address)"
ECS_IP="$(terraform output -raw public_ip)"
log "ECS=$ECS_IP  RDS=$RDS_ADDR  DCS=$DCS_ADDR"

# ── 2. 传代码 + 装依赖 + 起服务 ─────────────────────────────────
log "等待 ECS 起来（cloud-init 要装 Python）"
for i in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       "root@$ECS_IP" 'test -d /opt/vigil' 2>/dev/null; then
    break
  fi
  sleep 10
done

log "打包上传代码（排除本地数据与虚拟环境）"
TARBALL="$(mktemp -t vigil-XXXXXX.tar.gz)"
tar -czf "$TARBALL" -C "$REPO_ROOT" \
  --exclude='.git' --exclude='data' --exclude='.venv' --exclude='node_modules' \
  --exclude='web/dist' --exclude='__pycache__' --exclude='.pytest_cache' \
  vigil web config deploy pyproject.toml README.md LICENSE demo
scp -o StrictHostKeyChecking=no "$TARBALL" "root@$ECS_IP:/tmp/vigil.tar.gz"
rm -f "$TARBALL"

log "解包 + 建 venv + 装 cloud 依赖"
ssh -o StrictHostKeyChecking=no "root@$ECS_IP" bash -s <<REMOTE
set -euo pipefail
mkdir -p /opt/vigil && tar -xzf /tmp/vigil.tar.gz -C /opt/vigil
cd /opt/vigil
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# ⚠️ 装 `[cloud]` 那组：pymysql + redis。它们是**可选依赖** ——
#    本地复现不需要，云端必须。
.venv/bin/pip install -q -e '.[cloud]'
chown -R vigil:vigil /opt/vigil
REMOTE

log "写 /etc/vigil/vigil.env（权限 600，含 DSN 与密码）"
# ⚠️ 密码在这里经 ssh stdin 传输，**不进任何文件、不进 shell 历史**
#    （heredoc 走 stdin，不是命令行参数 —— 命令行参数会出现在 ps 里）。
ssh -o StrictHostKeyChecking=no "root@$ECS_IP" bash -s <<REMOTE
set -euo pipefail
cat > /etc/vigil/vigil.env <<ENVEOF
# VIGIL 云端环境（root:600）。**这个文件不在 git 里、也不该被截图。**
VIGIL_RDS_DSN=mysql://root:${TF_VAR_rds_password}@${RDS_ADDR}:3306/vigil
VIGIL_REDIS_URL=redis://:${TF_VAR_dcs_password}@${DCS_ADDR}:6379/0
ENVEOF
chmod 600 /etc/vigil/vigil.env
chown root:root /etc/vigil/vigil.env
REMOTE

log "建 RDS 表结构"
# ⚠️ schema.sql 在 RDS 上跑一次即可（CREATE TABLE IF NOT EXISTS 幂等）
ssh -o StrictHostKeyChecking=no "root@$ECS_IP" bash -s <<'REMOTE'
set -euo pipefail
# 用 venv 里的 python 跑 DDL —— 比在 ECS 上装 mysql 客户端干净
cd /opt/vigil
.venv/bin/python - <<'PY'
import os, pymysql, pathlib
dsn = os.environ["VIGIL_RDS_DSN"]
from vigil.db import _parse_dsn
conn = pymysql.connect(**_parse_dsn(dsn).copy() | {"database": None})
sql = pathlib.Path("deploy/mysql/schema.sql").read_text(encoding="utf-8")
with conn.cursor() as cur:
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s and not s.startswith("--"):
            cur.execute(s)
conn.commit()
print("schema ok")
PY
REMOTE

log "装 systemd 单元并启动"
ssh -o StrictHostKeyChecking=no "root@$ECS_IP" bash -s <<'REMOTE'
set -euo pipefail
cp /opt/vigil/deploy/systemd/*.service /etc/systemd/system/
cp /opt/vigil/deploy/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now vigil-serve.service
systemctl enable --now vigil-daily.timer
sleep 3
systemctl is-active vigil-serve.service
REMOTE

# ── 3. 验收 ─────────────────────────────────────────────────────
log "验收：健康检查 + 首页"
sleep 5
curl -fsS "http://$ECS_IP:8000/api/groups" >/dev/null \
  && log "✅ /api/groups 可达" \
  || die "健康检查失败 —— 上 ECS 看 \`journalctl -u vigil-serve -n 50\`"

cat <<MSG

═══════════════════════════════════════════════════════════════
 ✅ 部署完成

   Demo 地址：http://$ECS_IP:8000
   RDS：$RDS_ADDR:3306      DCS：$DCS_ADDR:6379

   ⚠️ **下一步（必做）**：
     1. 本机跑 \`python -m vigil refine\` 后 \`python -m vigil sync\`
        把合成数据推上 RDS —— 否则 Demo 页面是空的
     2. 截图存档（材料要用）
     3. 演示完 **立刻销毁**：
            ./deploy/deploy.sh --destroy
        ECS/EIP/RDS/DCS 都按小时计费，忘了销毁是这类项目最贵的低级错误。
═══════════════════════════════════════════════════════════════
MSG
