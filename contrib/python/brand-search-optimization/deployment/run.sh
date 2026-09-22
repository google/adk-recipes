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

set -x
set -e

# Determine the directory where this script resides
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
# Assume the project root directory is one level up from the script's directory
ROOT_DIR=$(dirname "$SCRIPT_DIR")

install_prereqs(){
    echo "--- Changing to root directory ($ROOT_DIR) to install prerequisites ---"
    # Execute poetry install within a subshell, changing directory first
    (cd "$ROOT_DIR" && poetry install)
    echo "--- Prerequisites installation finished ---"
}

populate_bq_data(){
    echo "--- Changing to root directory ($ROOT_DIR) to populate BigQuery data ---"
    # Execute the python script from the root directory within a subshell
    (cd "$ROOT_DIR" && python -m deployment.bq_populate_data)
    echo "--- BigQuery population finished ---"
}

main(){
    install_prereqs
    populate_bq_data
}

main

exit 0
