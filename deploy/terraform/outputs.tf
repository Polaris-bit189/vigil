output "instance_id" {
  value = huaweicloud_compute_instance.app.id
}

output "public_ip" {
  value = huaweicloud_vpc_eip.app.address
}

output "private_ip" {
  value = try(huaweicloud_compute_instance.app.network[0].fixed_ip_v4, null)
}

output "security_group_id" {
  value = huaweicloud_networking_secgroup.app.id
}

output "vpc_id" {
  value = huaweicloud_vpc.app.id
}

output "subnet_id" {
  value = huaweicloud_vpc_subnet.app.id
}

# ── RDS / DCS 连接信息 ────────────────────────────────────────────
#
# ⚠️ 这两个 output 会被 `scripts/collect_resource_info.py` 读走，
#    注入成应用的环境变量。**只输出地址与端口，不输出密码** ——
#    密码由部署脚本从 TF_VAR_ 环境变量另路传递。
#
# ⚠️ 都是**内网地址**（RDS/DCS 没开公网）：ECS 走内网访问。
#    本机若要直连，需要跳板机或 VPN —— 那是有意的摩擦。

output "rds_address" {
  value       = huaweicloud_rds_instance.vigil.private_ips[0]
  description = "RDS 内网地址"
}

output "rds_port" {
  value = huaweicloud_rds_instance.vigil.port
}

output "dcs_address" {
  value       = huaweicloud_dcs_instance.vigil.private_ips[0]
  description = "DCS(Redis) 内网地址"
}

output "dcs_port" {
  value = huaweicloud_dcs_instance.vigil.port
}
