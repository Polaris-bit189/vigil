resource "huaweicloud_networking_secgroup" "app" {
  name        = var.security_group_name
  description = "auto-deploy security group for ${var.app_name}"
}

resource "huaweicloud_networking_secgroup_rule" "ssh" {
  security_group_id = huaweicloud_networking_secgroup.app.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = var.ssh_cidr
}

resource "huaweicloud_networking_secgroup_rule" "http" {
  count             = var.open_http ? 1 : 0
  security_group_id = huaweicloud_networking_secgroup.app.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 80
  port_range_max    = 80
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "huaweicloud_networking_secgroup_rule" "https" {
  count             = var.open_https ? 1 : 0
  security_group_id = huaweicloud_networking_secgroup.app.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "huaweicloud_networking_secgroup_rule" "app" {
  count             = contains([22, 80, 443], var.app_port) ? 0 : 1
  security_group_id = huaweicloud_networking_secgroup.app.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = var.app_port
  port_range_max    = var.app_port
  remote_ip_prefix  = var.app_public_cidr
}

resource "huaweicloud_networking_secgroup_rule" "egress_all" {
  security_group_id = huaweicloud_networking_secgroup.app.id
  direction         = "egress"
  ethertype         = "IPv4"
  remote_ip_prefix  = "0.0.0.0/0"
}
