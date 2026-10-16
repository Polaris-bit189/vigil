# 云端部署

> 目标：华为云上跑一个**公网可访问、可交互**的 Demo，且**真的用上 RDS 与 DCS**。

---

## 一条命令

```bash
./deploy/deploy.sh              # 完整部署
./deploy/deploy.sh --plan-only  # 只看计划，不动资源
./deploy/deploy.sh --destroy    # 销毁（**演示完立刻做**）
```

## 前置三项（卡在账号操作上，只能你自己做）

```bash
# ① AK/SK —— 华为云控制台 → 我的凭证 → 访问密钥
export HW_ACCESS_KEY="..."
export HW_SECRET_KEY="..."

# ② 数据库密码 —— 走 TF_VAR_，绝不写进 terraform.tfvars
export TF_VAR_rds_password="$(openssl rand -base64 24)"
export TF_VAR_dcs_password="$(openssl rand -base64 24)"

# ③ Terraform ≥ 1.8
terraform -version
```

⚠️ **AK/SK 与密码只走环境变量。** 它们不进 git、不进 README、不进 PPT、不进截图。
`deploy/terraform/*.tfstate` 含密码明文 ⇒ 已被 `.gitignore` 挡住。

---

## 这一套建了什么

| 资源 | 用途 | Terraform |
|---|---|---|
| **ECS** + EIP | 应用服务（Web / PWA） | 从官方 auto-deploy 模板 fork |
| **RDS for MySQL** | 结构化条目与日报 | `rds.tf` |
| **DCS for Redis** | 缓存 + 分布式锁 | `dcs.tf` |
| VPC / 子网 / 安全组 | 网络 | 同上 |

**RDS 与 DCS 不开公网**（只有内网地址）—— ECS 走内网访问。
理由：它们里面是同学们的消息；开公网等于暴露到互联网。

---

## 为什么 RDS/DCS 要自己写 Terraform

官方 `auto-deploy` Skill 的 `SKILL.md` 明写：

> **Out of scope:** `RDS/Redis/OBS/MQ/ELB/CCE/Kubernetes stacks`

而赛题任务第 2 条要求「关联 RDS、REDIS 等华为云服务的**快速开通创建**」。
`auto-deploy` **只创建** VPC / 子网 / 安全组 / ECS / EIP / KPS 密钥对。

**缺口比预想小** —— 华为云 Terraform provider 原生支持：
- `huaweicloud_rds_instance`（MySQL / PostgreSQL / SQLServer）
- `huaweicloud_dcs_instance`（`engine = "Redis"`，**DCS 就是华为云的 Redis**）

且 Skill 的 `.tfrc` 已配好 provider 镜像源。所以这是**声明式工作**。

### 为什么 Terraform 根是 `deploy/terraform/` 而不是 Skill 的目录

Skill 用 `.auto-deploy/terraform/` 当工作目录，**重跑会覆盖**。
我们从它的 `templates/terraform/ecs-web` **fork** 出来作为自己的根，
这样 `terraform destroy` 能一次性清干净 —— **不会留下一台按小时计费的 RDS**。

（Fork 而非 patch：`templates/` 是 Skill 的只读资产，升级时会被覆盖。）

---

## 部署脚本干了什么

```
① 前置检查（terraform / AK / 密码）—— 失败就说清缺什么、怎么补
② terraform init / apply   →  ECS + EIP + SG + VPC + RDS + DCS
③ scp 代码到 ECS，建 venv，装 '.[cloud]'（pymysql + redis）
④ 写 /etc/vigil/vigil.env（root:600，经 ssh stdin —— 不进 ps / 不进历史）
⑤ 在 RDS 上跑 schema.sql
⑥ 装 systemd 单元，enable --now
⑦ 验收：curl /api/groups
```

### 一个细节：密码怎么传

密码经 **ssh 的 stdin**（heredoc）传，不走命令行参数 ——
命令行参数会出现在 `ps` 输出里，同机器上任何用户都能看到。

---

## 部署之后（**必做**）

```bash
# 本机：跑管线并同步到 RDS
python -m vigil demo       # 或 ingest —— 建库
python -m vigil refine     # 抽取（需要 LLM 端点）
python -m vigil sync       # 推四张表到 RDS
```

⚠️ 不做这一步的话，Demo 页面是**空的** —— 云端只有表结构，没有数据。

```bash
# 验一验推了什么（不写库）
python -m vigil sync --dry-run
```

## 演示完立刻销毁

```bash
./deploy/deploy.sh --destroy
```

然后到控制台**复核**：ECS / EIP / RDS / DCS 都应消失。

ECS、EIP、RDS、DCS **都按小时计费**，而"忘了销毁"是这类项目
最贵的低级错误 —— 一晚就是几十块，一周就是几百。

---

## 排查

| 现象 | 看哪里 |
|---|---|
| 服务起不来 | `ssh root@$ECS_IP journalctl -u vigil-serve -n 50` |
| 连不上 RDS | 安全组是否放行 3306（**内网**，不是公网）；DSN 里的地址是不是内网地址 |
| `/api/items` 返回空 | 本机的 `vigil sync` 跑了没有 |
| Redis 没生效 | `curl /api/health` 看 `cache` 字段；或控制台看 DCS 连接数 |
| Terraform 卡在 RDS 创建 | **正常** —— 托管数据库要几分钟到十几分钟 |
