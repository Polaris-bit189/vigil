# RDS for MySQL —— 华为云托管的数据库，放结构化条目与日报。
#
# 为什么要自己写（官方 auto-deploy Skill 不覆盖它）：
#   其 SKILL.md 明写 `Out of scope: RDS/Redis/OBS/MQ/ELB/CCE/Kubernetes stacks`，
#   而赛题任务第 2 条要求「关联 RDS、REDIS 等华为云服务的快速开通创建」。
#   缺口比预想小 —— 华为云 Terraform provider **原生支持**，所以是声明式工作。
#
# ⚠️ `flavor` 用 data source **动态查**，不硬编码。
#    硬编码的规格名会随区域/可用区变化而失效（实测：同一个规格名在
#    cn-north-4 有、换个区域就没有），而报错会指向"规格不存在"，
#    排查方向完全错——看起来像代码写错，其实是区域差异。
data "huaweicloud_rds_flavors" "mysql" {
  db_type       = "MySQL"
  db_version    = var.rds_db_version
  instance_mode = "single"
  vcpus         = var.rds_vcpus
}

resource "huaweicloud_rds_instance" "vigil" {
  name              = "${var.app_name}-rds"
  flavor            = data.huaweicloud_rds_flavors.mysql.flavors[0].name
  vpc_id            = huaweicloud_vpc.app.id
  subnet_id         = huaweicloud_vpc_subnet.app.id
  security_group_id = huaweicloud_networking_secgroup.app.id
  availability_zone = [var.availability_zone]

  db {
    type    = "MySQL"
    version = var.rds_db_version
    port    = 3306
    # ⚠️ 密码走 `TF_VAR_rds_password` 环境变量，**不写 tfvars、不进 git**。
    #    Terraform state 里仍是明文 ⇒ state 已被 .gitignore 挡住。
    password = var.rds_password
  }

  # 演示用最小盘。云盘类型 CLOUDSSD 是通用型，够用且便宜。
  volume {
    type = "CLOUDSSD"
    size = var.rds_volume_size
  }

  # 备份策略：演示环境保留 7 天即可 —— 它**不计费**（RDS 自带），
  # 但没有它时手工恢复无从下手。设一个下限值而不是关掉。
  backup_strategy {
    start_time = "02:00-03:00"
    keep_days  = 7
  }

  # ⚠️ 只开内网访问（不开公网）：
  #    · ECS 与 RDS 在同一 VPC，走内网即可
  #    · 开公网等于把数据库暴露到互联网，而它里面是同学们的消息
  #    · 也让"本地 → RDS"的同步必须经 VPN/跳板，是个有意的摩擦
  tags = {
    app     = var.app_name
    purpose = "ict-contest-demo"
  }
}
