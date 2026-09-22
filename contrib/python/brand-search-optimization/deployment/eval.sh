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

prepare(){
    touch __init__.py
    export PYTHONPATH="$PYTHONPATH:."
}

run_eval(){
    adk eval \
        brand_search_optimization \
        eval/data/eval_data1.evalset.json \
        --config_file_path eval/data/test_config.json
}

main(){
    echo "
    You must be inside brand-search-optimization dir and then
    # sh deployment/eval.sh
    "
    prepare
    run_eval
}

main

exit 0
