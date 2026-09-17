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

variable "subject_id" {
  description = "The one person this deployment is for. Seeded by seed.py."
  type        = string
  default     = "sunita"
}

variable "max_tier" {
  description = "How many times the circle widens before the final fallback."
  type        = number
  default     = 3
}

variable "sender" {
  description = "From header on every email. The domain must be a verified SES identity."
  type        = string
  default     = "Pukaar <alert@aryangorde.com>"
}

variable "sender_domain" {
  type    = string
  default = "aryangorde.com"
}
