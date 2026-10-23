variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "netloom"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "project must be 3-21 lowercase letters, digits or dashes."
  }
}

variable "environment" {
  description = "Deployment stage, e.g. dev or prod."
  type        = string
  default     = "dev"
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

variable "max_subnets_per_network" {
  description = "Upper bound on subnets a single request may create."
  type        = number
  default     = 16
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "api_throttle_rate" {
  description = "Steady-state requests per second across the API."
  type        = number
  default     = 20
}

variable "api_throttle_burst" {
  type    = number
  default = 40
}

variable "cors_allowed_origins" {
  description = "Browser origins allowed to call the API. Empty disables CORS."
  type        = list(string)
  default     = []
}

variable "invited_users" {
  description = "E-mail addresses to invite into the user pool. Cognito e-mails each a temporary password."
  type        = list(string)
  default     = []
}

variable "access_token_minutes" {
  type    = number
  default = 60
}

variable "protect_data" {
  description = "Enable deletion protection on the user pool and table (recommended outside dev)."
  type        = bool
  default     = false
}
