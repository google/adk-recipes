#!/bin/bash
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

# Fix opencv: deepface pulls opencv-python which needs libGL.so.1 (not in container).
# Replace with headless version that provides the same cv2 without GUI deps.
# Use the venv pip — the app runs from /code/.venv/
/code/.venv/bin/pip uninstall -y opencv-python opencv-contrib-python 2>/dev/null
/code/.venv/bin/pip install --force-reinstall --no-deps opencv-python-headless==4.13.0.92

# Install insightface from pre-built wheel (requires g++ if built from source)
if compgen -G "/code/startup_scripts/insightface*.whl" > /dev/null; then
    /code/.venv/bin/pip install --no-deps /code/startup_scripts/insightface*.whl
fi
