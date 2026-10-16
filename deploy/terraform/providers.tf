terraform {
  required_version = ">= 1.9.0"

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.90.0"
    }
  }
}

provider "huaweicloud" {
  region = var.region
}
