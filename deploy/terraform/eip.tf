resource "huaweicloud_vpc_eip" "app" {
  publicip {
    type = var.eip_type
  }

  bandwidth {
    name        = var.bandwidth_name
    size        = var.bandwidth_size
    share_type  = var.bandwidth_share_type
    charge_mode = var.bandwidth_charge_mode
  }
}

resource "huaweicloud_compute_eip_associate" "app" {
  instance_id = huaweicloud_compute_instance.app.id
  public_ip   = huaweicloud_vpc_eip.app.address
}
