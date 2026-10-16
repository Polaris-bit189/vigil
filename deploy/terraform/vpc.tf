resource "huaweicloud_vpc" "app" {
  name = var.vpc_name
  cidr = var.vpc_cidr
}

resource "huaweicloud_vpc_subnet" "app" {
  name          = var.subnet_name
  cidr          = var.subnet_cidr
  gateway_ip    = var.subnet_gateway_ip
  vpc_id        = huaweicloud_vpc.app.id
  primary_dns   = "100.125.1.250"
  secondary_dns = "100.125.21.250"
}
