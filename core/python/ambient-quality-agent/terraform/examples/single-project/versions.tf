# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

terraform {
  required_version = ">= 1.4"

  # Empty on purpose. The local backend is the default with or without this
  # block, but declaring it is what makes `init -backend-config="path=..."`
  # take effect: with no block the flag is accepted and silently ignored. The
  # agents-cli extension relies on relocating the state out of the vendored
  # copy of this repository, which `agents-cli install` deletes and recreates.
  backend "local" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.13, < 8.0"
    }
  }
}
