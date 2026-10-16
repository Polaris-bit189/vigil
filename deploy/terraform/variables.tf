variable "region" {
  type = string
}

variable "security_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "availability_zone" {
  type = string
}

variable "app_name" {
  type = string
}

variable "vpc_name" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_name" {
  type = string
}

variable "subnet_cidr" {
  type = string
}

variable "subnet_gateway_ip" {
  type = string
}

variable "security_group_name" {
  type = string
}

variable "instance_name" {
  type = string
}

variable "instance_flavor_name" {
  type = string
}

variable "instance_image_name" {
  type        = string
  description = "Supports regular expression matching."
}

variable "system_disk_type" {
  type = string
}

variable "system_disk_size" {
  type = number
}

variable "keypair_name" {
  type = string
}

variable "keypair_public_key" {
  type      = string
  sensitive = true
}

variable "eip_type" {
  type = string
}

variable "bandwidth_name" {
  type = string
}

variable "bandwidth_size" {
  type = number
}

variable "bandwidth_share_type" {
  type = string
}

variable "bandwidth_charge_mode" {
  type = string
}

variable "ssh_cidr" {
  type = string
}

variable "app_port" {
  type = number
}

variable "app_public_cidr" {
  # ⚠️ **默认对全网开放**（2026-10-16 复核时确认这是有意的，不是拍脑袋）：
  # 评委要能从公网打开 Demo 页面，所以应用端口必须可达。
  #
  # ⚠️ **但部署后请自己收紧**：`vigil serve` 本身**没有鉴权**（它是单用户的
  # 本地工具，见 `api.py` 的模块 docstring）。Demo 阶段公开可接受，
  # 而一旦里面有真实数据，就该把它改成你自己的出口 IP：
  #     terraform apply -var="app_public_cidr=1.2.3.4/32"
  # 或干脆关掉应用端口直连、只留 80/443 走反向代理。
  type    = string
  default = "0.0.0.0/0"
}

variable "open_http" {
  type    = bool
  default = true
}

variable "open_https" {
  type    = bool
  default = true
}

# ── RDS / DCS（本次新增：赛题要求的「关联开通 RDS、REDIS」）──────────
#
# ⚠️ 密码走 `TF_VAR_rds_password` / `TF_VAR_dcs_password` **环境变量**：
#       export TF_VAR_rds_password="$(openssl rand -base64 24)"
#    绝不写进 terraform.tfvars（那份要进 git 的话就是泄漏）。
#    Terraform state 里仍是明文 ⇒ *.tfstate 已被 .gitignore 挡住。

variable "rds_password" {
  type      = string
  sensitive = true
  description = "RDS 管理员密码。走 TF_VAR_rds_password 环境变量注入。"
}

variable "rds_db_version" {
  type    = string
  default = "8.0"
}

variable "rds_vcpus" {
  type        = number
  default     = 2
  description = "用来动态查 flavor。演示用 2 核足够。"
}

variable "rds_volume_size" {
  type        = number
  default     = 40
  description = "GB。演示数据量小，40 是下限档。"
}

variable "dcs_password" {
  type      = string
  sensitive = true
  description = "DCS(Redis) 密码。走 TF_VAR_dcs_password 环境变量注入。"
}

variable "dcs_engine_version" {
  type    = string
  default = "5.0"
}

variable "dcs_capacity" {
  type        = number
  default     = 1
  description = "GB。单机 1GB 足够缓存日报，且是最低计费档。"
}
