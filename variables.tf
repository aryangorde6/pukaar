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

variable "telegram_bot_token" {
  description = "Her Telegram bot's token, from @BotFather. Set in terraform.tfvars (gitignored); empty means no Telegram."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_chat_ids" {
  description = "contact_id => Telegram chat id, for the people on her list who have started the bot. seed.sh reads it."
  type        = map(string)
  default     = {}
}

variable "checkin_schedule" {
  description = "When the weekly check-in goes out (EventBridge Scheduler cron, IST). Empty disables it. One hour a week is one hour learned; the hour is the operator's choice."
  type        = string
  default     = "cron(0 18 ? * WED *)"
}

variable "operator_email" {
  description = "Who is emailed when an escalation fails. Set in terraform.tfvars; empty means nobody is."
  type        = string
  default     = ""
}
