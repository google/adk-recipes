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

import { toast } from "sonner";

type Opts = { description?: string };

// The app's toast API. Call sites import `notify` (never `sonner` directly),
// which keeps toasts mockable in unit tests and swappable behind one abstraction.
export const notify = {
  success: (msg: string, opts?: Opts) => toast.success(msg, opts),
  error: (msg: string, opts?: Opts) => toast.error(msg, opts),
  info: (msg: string, opts?: Opts) => toast.info(msg, opts),
  message: (msg: string, opts?: Opts) => toast(msg, opts),
};
