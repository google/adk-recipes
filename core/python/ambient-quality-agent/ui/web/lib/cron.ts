// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * "0 12 * * *" reads as glitched asterisks. Only the fixed-time daily case,
 * which the default is, is translated; anything else shows the crontab it
 * actually is rather than a guess at its meaning.
 */
export function formatCron(cron: string): string {
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron.trim());
  if (!daily) return cron;
  const [, minute, hour] = daily;
  return `daily at ${hour.padStart(2, "0")}:${minute.padStart(2, "0")} UTC`;
}
