data "huaweicloud_images_image" "selected" {
  name_regex  = var.instance_image_name
  flavor_id   = var.instance_flavor_name
  most_recent = true
}
