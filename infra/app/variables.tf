variable "project" {
  type    = string
  default = "netloom"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  description = "AWS region for the API and for the VPCs it creates."
  type        = string
  default     = "eu-central-1"
}

variable "repository" {
  description = "Source repository, recorded as a tag for traceability."
  type        = string
  default     = "unknown"
}

variable "invited_users" {
  description = "E-mail addresses to invite into the user pool."
  type        = list(string)
  default     = []
}

variable "cors_allowed_origins" {
  type    = list(string)
  default = []
}

variable "protect_data" {
  type    = bool
  default = false
}

variable "max_subnets_per_network" {
  type    = number
  default = 16
}

variable "log_retention_days" {
  type    = number
  default = 30
}
