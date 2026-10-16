# DCS for Redis —— 华为云的 Redis 服务。
#
# ⚠️ 赛题里写的 "REDIS" 指的就是它：DCS = Distributed Cache Service，
#    引擎可以选 Redis / Memcached。这里 engine = "Redis"。
#
# 用途（三样，不多不少）：
#   1. 日报列表/详情的缓存（TTL 300s，写完后版本号自增失效）
#   2. 搜索结果缓存（TTL 60s）
#   3. 分布式写锁（SET NX PX + Lua 释放）
#
# ⚠️ 如实说明：本次是**单 ECS 部署**，进程内锁与分布式锁行为等价。
#    引入 DCS 锁的价值是满足赛题要求 + 让"水平扩容时锁语义不退化为无锁"
#    这件事有现成实现。**讲清楚比假装需要强。**
data "huaweicloud_dcs_flavors" "redis" {
  cache_mode     = "single"
  engine         = "Redis"
  engine_version = var.dcs_engine_version
  capacity       = var.dcs_capacity
}

resource "huaweicloud_dcs_instance" "vigil" {
  name               = "${var.app_name}-dcs"
  engine             = "Redis"
  engine_version     = var.dcs_engine_version
  capacity           = var.dcs_capacity
  flavor             = data.huaweicloud_dcs_flavors.redis.flavors[0].name
  vpc_id             = huaweicloud_vpc.app.id
  subnet_id          = huaweicloud_vpc_subnet.app.id
  security_group_id  = huaweicloud_networking_secgroup.app.id
  availability_zones = [var.availability_zone]
  password           = var.dcs_password

  # ⚠️ 与 RDS 同理：**不开公网**。ECS 走内网访问。
  tags = {
    app     = var.app_name
    purpose = "ict-contest-demo"
  }
}
