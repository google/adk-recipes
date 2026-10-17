# Copyright 2026 Google LLC
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
"""Invoice downloads and archiving. INTENTIONALLY VULNERABLE sample code."""

import os
import subprocess


def read_invoice(invoice_dir: str, name: str) -> bytes:
    """Returns the bytes of an invoice file for the download endpoint."""
    # VULNERABLE: `name` comes from the URL and may contain "../".
    path = os.path.join(invoice_dir, name)
    with open(path, "rb") as f:
        return f.read()


def archive_invoices(invoice_dir: str, month: str, out_dir: str) -> str:
    """Packs one month's invoices into a .tar.gz for the finance team.

    Args:
        invoice_dir: Directory containing per-month invoice folders.
        month: Month in YYYY-MM form, taken from the request.
        out_dir: Directory where the archive is written.

    Returns:
        Path of the created archive.
    """
    archive = os.path.join(out_dir, f"invoices-{month}.tar.gz")
    # VULNERABLE: `month` is interpolated into a shell command.
    subprocess.run(  # noqa: S602 -- intentional sample vulnerability
        f"tar -czf {archive} -C {invoice_dir} {month}",
        shell=True,
        check=True,
    )
    return archive
