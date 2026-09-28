# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared generic utilities for parallel processing and common operations."""

# Standard library imports
from concurrent.futures import ThreadPoolExecutor

# Third-party imports
from tqdm import tqdm


def predict_parallel(
    to_predict, predict_function, max_workers=8, show_progress_bar=True
):
    """Execute a function in parallel across multiple inputs using ThreadPoolExecutor.

    Args:
        to_predict: Iterable of inputs to process
        predict_function: Function to apply to each input
        max_workers: Maximum number of parallel workers (default: 8)
        show_progress_bar: Whether to display a tqdm progress bar (default: True)

    Returns:
        list: Results from applying predict_function to each input

    """
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        if show_progress_bar:
            for result in tqdm(
                pool.map(predict_function, to_predict), total=len(to_predict)
            ):
                results.append(result)
        else:
            for result in pool.map(predict_function, to_predict):
                results.append(result)
    return results
