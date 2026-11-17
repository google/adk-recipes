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

"""Reviewer tools that read the trajectory JSON of one review.

`jq.JqQueryTool` runs a jq expression over the trajectory JSON in a child
process. `grep_read.GrepTool` searches values under a path with a regular
expression, and `grep_read.ReadTool` returns the lines of a value. Both
address values by a path of keys, indexes and ``[*]`` steps in jq syntax (such
as ``.agents[0].instruction``) or by an event ID (such as ``t0.e8``), and both
run in-process. `toolsets` in the parent package selects the tools for each
review.

Every tool returns ``{"error": text}`` on a recoverable error, so callers
distinguish output from an error by the key. On success, the `grep_read` tools
return ``{"result": text}`` (which ADK passes to Claude as raw text), and the
jq tool returns ``{"output": text}``.
"""
