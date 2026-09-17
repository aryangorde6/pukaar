variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "profile" {
  type    = string
  default = "hackathon"
}

variable "prefix" {
  type    = string
  default = "pukaar"
}

variable "wait_s" {
  description = "Seconds to wait for a claim before widening the circle. 60 in production, 10 for the demo."
  type        = number
  default     = 60
}
