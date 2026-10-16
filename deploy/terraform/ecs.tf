resource "huaweicloud_compute_instance" "app" {
  name              = var.instance_name
  availability_zone = var.availability_zone
  flavor_name       = var.instance_flavor_name
  image_id          = data.huaweicloud_images_image.selected.id
  key_pair          = huaweicloud_kps_keypair.deploy.name
  security_group_ids = [
    huaweicloud_networking_secgroup.app.id
  ]

  network {
    uuid = huaweicloud_vpc_subnet.app.id
  }

  system_disk_type = var.system_disk_type
  system_disk_size = var.system_disk_size
}
