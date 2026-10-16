resource "huaweicloud_kps_keypair" "deploy" {
  name       = var.keypair_name
  public_key = var.keypair_public_key
}
