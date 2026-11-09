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

# ruff: noqa: E402, PLW1510
"""
Comprehensive End-to-End Integration & Regression Suite for adk-tokenomics-plugin.

Exercises the full system across SDK, ADK callbacks, atomic trace export & session rotation,
cross-process hydration, CLI (`profile`, `assert`, `finops`), live `ThreadingHTTPServer` APIs,
Node VM execution of `web/app.js`, BigQuery dual-source streaming/querying, and package integrity
without mocking internal project logic.
"""

import asyncio
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import tomllib
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from adk_tokenomics_plugin import (
    BigQuerySink,
    FinOpsAggregator,
    InMemorySink,
    PricingEngine,
    TokenControlTower,
    TokenomicsConfig,
    TokenTracker,
    provision_bigquery_table,
    track_tokens,
)
from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli
from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery
from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler

_CREATE_TABLE_DDL_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?`([^`]+)`\s*\((.*?)\)\s*(?:PARTITION|CLUSTER|OPTIONS|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_VIEW_DDL_RE = re.compile(
    r"CREATE\s+(OR\s+REPLACE\s+)?VIEW\s+(IF\s+NOT\s+EXISTS\s+)?`([^`]+)`\s+AS\s+(.*)",
    re.IGNORECASE | re.DOTALL,
)
_DDL_COL_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z0-9_<>]+)"
)
_FROM_TARGET_RE = re.compile(r"FROM\s+`([^`]+)`", re.IGNORECASE)
_SELECT_STAR_RE = re.compile(r"SELECT\s+\*\s+FROM", re.IGNORECASE)
_LIMIT_OFFSET_RE = re.compile(r"LIMIT\s+(\d+)\s+OFFSET\s+(\d+)", re.IGNORECASE)
_LIMIT_ONLY_RE = re.compile(r"LIMIT\s+(\d+)", re.IGNORECASE)
_SAVED_DOLLARS_RE = re.compile(r"Saved \$([0-9.]+)")
_SINGLE_QUOTED_LIT_RE = re.compile(r"'[^']*'")
_WORD_ATTR_RE = re.compile(
    r"\b(?:attributes|content|event_type)\b", re.IGNORECASE
)
_WORD_USAGE_PROMPT_RE = re.compile(r"\busage_prompt_tokens\b", re.IGNORECASE)
_AS_USAGE_PROMPT_RE = re.compile(r"\bAS\s+usage_prompt_tokens\b", re.IGNORECASE)
_WORD_USAGE_TOOL_RE = re.compile(r"\busage_tool_use_tokens\b", re.IGNORECASE)
_AS_USAGE_TOOL_RE = re.compile(r"\bAS\s+usage_tool_use_tokens\b", re.IGNORECASE)


NODE_APP_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const baseUrl = process.env.E2E_BASE_URL;
const htmlPath = process.env.E2E_INDEX_HTML;
const appJsPath = process.env.E2E_APP_JS;
const enableChart = process.env.E2E_ENABLE_CHART === '1';
const runBqExportCsv = process.env.E2E_RUN_BQ_CSV === '1';
const targetSession = process.env.E2E_SESSION_ID || 'latest';

const htmlSrc = fs.readFileSync(htmlPath, 'utf8');
const appJsSrc = fs.readFileSync(appJsPath, 'utf8');

const pendingPromises = new Set();
function trackPromise(p) {
  pendingPromises.add(p);
  p.finally(() => pendingPromises.delete(p));
  return p;
}

class ClassList {
  constructor() { this._set = new Set(); }
  add(...names) { names.forEach(n => this._set.add(n)); }
  remove(...names) { names.forEach(n => this._set.delete(n)); }
  contains(n) { return this._set.has(n); }
  toggle(n) { if (this._set.has(n)) { this._set.delete(n); return false; } this._set.add(n); return true; }
}

function stripTags(s) {
  return String(s || '').replace(/<[^>]*>/g, '').trim();
}

function parseAttrs(attrStr) {
  const attrs = {};
  const re = /([a-zA-Z0-9_:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let m;
  while ((m = re.exec(attrStr || '')) !== null) {
    attrs[m[1]] = m[2] !== undefined ? m[2] : m[3];
  }
  return attrs;
}

class MiniElement {
  constructor(tagName, id = '') {
    this.tagName = String(tagName || 'DIV').toUpperCase();
    this.id = id || '';
    this.className = '';
    this.classList = new ClassList();
    this.style = {};
    this.attributes = {};
    if (id) this.attributes.id = id;
    this.children = [];
    this.parentElement = null;
    this._listeners = {};
    this._textContent = '';
    this._innerHTML = '';
    this.value = this.tagName === 'SELECT' ? 'latest' : '';
    this.disabled = false;
    this.open = true;
    this.options = [];
  }

  setAttribute(k, v) {
    const val = String(v);
    this.attributes[k] = val;
    if (k === 'id') this.id = val;
    if (k === 'value') this.value = val;
  }

  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
  }

  get textContent() {
    if (this.children.length > 0 && !this._textContent) {
      return this.children.map(c => c.textContent).join(' ');
    }
    return this._textContent;
  }

  set textContent(val) {
    this._textContent = val === null || val === undefined ? '' : String(val);
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(html) {
    const str = html === null || html === undefined ? '' : String(html);
    this._innerHTML = str;
    this.children = [];
    if (!str.trim()) {
      if (this.tagName === 'SELECT') this.options = [];
      return;
    }

    if (this.tagName === 'SELECT') {
      const opts = [];
      const optRe = /<option([^>]*)>([\s\S]*?)<\/option>/gi;
      let om;
      while ((om = optRe.exec(str)) !== null) {
        const oa = parseAttrs(om[1]);
        const optEl = new MiniElement('OPTION');
        optEl.attributes = oa;
        optEl.value = oa.value !== undefined ? oa.value : stripTags(om[2]);
        optEl.textContent = stripTags(om[2]);
        opts.push(optEl);
      }
      this.options = opts;
      this.children = opts;
      return;
    }

    if (this.tagName === 'TR') {
      const tdRe = /<td([^>]*)>([\s\S]*?)<\/td>/gi;
      let tm;
      while ((tm = tdRe.exec(str)) !== null) {
        const td = new MiniElement('TD');
        td.attributes = parseAttrs(tm[1]);
        td._innerHTML = tm[2];
        td._textContent = stripTags(tm[2]);
        td.parentElement = this;
        this.children.push(td);
      }
      return;
    }

    if (this.tagName === 'TBODY') {
      const trRe = /<tr([^>]*)>([\s\S]*?)<\/tr>/gi;
      let rm;
      while ((rm = trRe.exec(str)) !== null) {
        const tr = new MiniElement('TR');
        tr.attributes = parseAttrs(rm[1]);
        tr.innerHTML = rm[2];
        tr.parentElement = this;
        this.children.push(tr);
      }
    }
  }

  appendChild(child) {
    child.parentElement = this;
    const existingIdx = this.children.indexOf(child);
    if (existingIdx >= 0) this.children.splice(existingIdx, 1);
    this.children.push(child);
    if (this === documentObj.body && child.tagName === 'A') {
      lastAppendedAnchor = child;
    }
    return child;
  }

  remove() {
    if (this.parentElement) {
      const idx = this.parentElement.children.indexOf(this);
      if (idx >= 0) this.parentElement.children.splice(idx, 1);
    }
  }

  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  }

  click() {
    if (this.tagName === 'A') {
      downloadedLinks.push({
        href: this.getAttribute('href'),
        download: this.getAttribute('download')
      });
    }
    const handlers = this._listeners['click'] || [];
    handlers.forEach(fn => fn({ target: this, stopPropagation() {}, preventDefault() {} }));
  }

  dispatchEvent(ev) {
    const t = ev && ev.type ? ev.type : '';
    const handlers = this._listeners[t] || [];
    handlers.forEach(fn => fn(Object.assign({ target: this, stopPropagation() {}, preventDefault() {} }, ev)));
  }

  closest(sel) {
    if (sel === 'table' && this._tableOwner) return this._tableOwner;
    let cur = this.parentElement;
    while (cur) {
      if (cur.tagName.toLowerCase() === String(sel).toLowerCase()) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  querySelector(sel) {
    if (sel === 'td[colspan]') {
      return this.children.find(c => c.tagName === 'TD' && c.getAttribute('colspan') !== null) || null;
    }
    if (sel === '.sort-indicator') {
      return this.children.find(c => c.className === 'sort-indicator') || null;
    }
    return null;
  }

  querySelectorAll(sel) {
    if (sel === 'tr') return this.children.filter(c => c.tagName === 'TR');
    if (sel === 'thead th' && this._headers) return this._headers;
    return [];
  }

  getContext() {
    return {
      canvas: this,
      clearRect() {},
      fillRect() {},
      beginPath() {},
      moveTo() {},
      lineTo() {},
      stroke() {}
    };
  }

  reset() {}
  scrollIntoView() {}
}

const elementsById = {};
let lastAppendedAnchor = null;
const downloadedLinks = [];
const blobStore = new Map();
let blobCounter = 0;

const idTagRe = /<([a-zA-Z0-9]+)\b[^>]*\bid="([^"]+)"[^>]*>/g;
let match;
while ((match = idTagRe.exec(htmlSrc)) !== null) {
  const tag = match[1];
  const id = match[2];
  const el = new MiniElement(tag, id);
  if (id === 'adk-session-select') {
    el.value = targetSession;
    const optLatest = new MiniElement('OPTION'); optLatest.value = 'latest';
    const optGlobal = new MiniElement('OPTION'); optGlobal.value = 'global';
    const optTarget = new MiniElement('OPTION'); optTarget.value = targetSession;
    el.options = [optLatest, optGlobal, optTarget];
  } else if (id === 'adk-app-select' || id === 'bq-filter-app') {
    el.value = 'all';
    const optAll = new MiniElement('OPTION'); optAll.value = 'all';
    el.options = [optAll];
  } else if (id === 'bq-filter-limit') {
    el.value = '50';
  }
  elementsById[id] = el;
}

['adk-live-table-body', 'session-turns-history-table-body', 'session-tools-table-body'].forEach(tbId => {
  const tbody = elementsById[tbId];
  if (tbody) {
    const tbl = new MiniElement('TABLE');
    const colCount = tbId === 'adk-live-table-body' ? 7 : (tbId === 'session-tools-table-body' ? 4 : 9);
    tbl._headers = Array.from({ length: colCount }, () => new MiniElement('TH'));
    tbody._tableOwner = tbl;
  }
});

const domListeners = {};
const documentObj = {
  body: new MiniElement('BODY'),
  getElementById(id) { return elementsById[id] || null; },
  createElement(tag) { return new MiniElement(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(type, fn) {
    if (!domListeners[type]) domListeners[type] = [];
    domListeners[type].push(fn);
  }
};

const localStore = new Map();
const localStorageStub = {
  getItem(k) { return localStore.has(k) ? localStore.get(k) : null; },
  setItem(k, v) { localStore.set(k, String(v)); },
  removeItem(k) { localStore.delete(k); }
};

const chartInstances = [];
class FakeChart {
  constructor(ctx, cfg) {
    this.ctx = ctx;
    this.config = cfg;
    this.data = cfg.data || { labels: [], datasets: [] };
    this.options = cfg.options || {};
    chartInstances.push(this);
  }
  update() {}
  resize() {}
  destroy() {}
}
FakeChart.defaults = { color: '#999', font: { family: 'sans-serif' } };

class TrackedBlob {
  constructor(parts, opts) {
    this.content = (parts || []).map(p => String(p)).join('');
    this.type = (opts && opts.type) || '';
  }
}

const urlStub = {
  createObjectURL(blob) {
    const key = `blob:node-e2e/${++blobCounter}`;
    blobStore.set(key, blob && blob.content !== undefined ? blob.content : String(blob));
    return key;
  },
  revokeObjectURL() {}
};

const runtimeErrors = [];

function wrappedFetch(input, init) {
  const rawUrl = String(input);
  const fullUrl = rawUrl.startsWith('http') ? rawUrl : `${baseUrl}${rawUrl.startsWith('/') ? '' : '/'}${rawUrl}`;
  const cleanInit = Object.assign({}, init || {});
  delete cleanInit.signal;
  const p = globalThis.fetch(fullUrl, cleanInit);
  return trackPromise(p);
}

const vmCtx = {
  window: null,
  document: documentObj,
  localStorage: localStorageStub,
  navigator: { clipboard: { writeText: () => Promise.resolve() } },
  fetch: wrappedFetch,
  AbortController: globalThis.AbortController,
  Blob: TrackedBlob,
  URL: urlStub,
  Chart: enableChart ? FakeChart : undefined,
  console: {
    log() {},
    warn() {},
    error(...args) { runtimeErrors.push(args.map(String).join(' ')); }
  },
  alert(msg) { runtimeErrors.push(`alert:${msg}`); },
  setTimeout(fn) { return 1; },
  clearTimeout() {},
  setInterval() { return 1; },
  clearInterval() {},
  encodeURIComponent,
  decodeURIComponent,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Set,
  Map,
  Math,
  Date,
  RegExp,
  JSON,
  Error,
  TypeError,
  parseInt,
  parseFloat,
  isNaN
};
vmCtx.window = vmCtx;

async function drainNetwork() {
  for (let round = 0; round < 20; round++) {
    if (pendingPromises.size === 0) {
      await new Promise(r => setTimeout(r, 15));
      if (pendingPromises.size === 0) break;
    }
    await Promise.allSettled(Array.from(pendingPromises));
    await new Promise(r => setTimeout(r, 10));
  }
}

(async () => {
  try {
    vm.createContext(vmCtx);
    vm.runInContext(appJsSrc, vmCtx, { filename: 'app.js' });
    (domListeners['DOMContentLoaded'] || []).forEach(fn => fn());
    await drainNetwork();

    if (targetSession !== 'latest' && elementsById['adk-session-select']) {
      elementsById['adk-session-select'].value = targetSession;
      elementsById['adk-session-select'].dispatchEvent({ type: 'change' });
      await drainNetwork();
    }

    if (runBqExportCsv && elementsById['tab-btn-bq']) {
      elementsById['tab-btn-bq'].click();
      await drainNetwork();
      if (elementsById['btn-bq-export-csv']) {
        elementsById['btn-bq-export-csv'].click();
      }
    }

    const liveRows = (elementsById['adk-live-table-body'] ? elementsById['adk-live-table-body'].children : []).map(tr => {
      const cells = tr.children || [];
      return {
        agent: cells[0] ? cells[0].getAttribute('data-sort-val') : '',
        turns: cells[1] ? Number(cells[1].getAttribute('data-sort-val')) : 0,
        fresh: cells[2] ? Number(cells[2].getAttribute('data-sort-val')) : 0,
        cached: cells[3] ? Number(cells[3].getAttribute('data-sort-val')) : 0,
        output: cells[4] ? Number(cells[4].getAttribute('data-sort-val')) : 0,
        thinking: cells[5] ? Number(cells[5].getAttribute('data-sort-val')) : 0,
        cost: cells[6] ? Number(cells[6].getAttribute('data-sort-val')) : 0
      };
    });

    const csvExports = downloadedLinks.map(item => {
      if (item.href && blobStore.has(item.href)) {
        return { download: item.download, csv: blobStore.get(item.href) };
      }
      if (item.href && item.href.startsWith('data:text/csv')) {
        const idx = item.href.indexOf(',');
        return { download: item.download, csv: idx >= 0 ? decodeURIComponent(item.href.slice(idx + 1)) : '' };
      }
      return { download: item.download, csv: '' };
    });

    const out = {
      runtimeErrors,
      chartCount: chartInstances.length,
      kpis: {
        totalCostText: elementsById['kpi-total-cost'] ? elementsById['kpi-total-cost'].textContent : '',
        costDescText: elementsById['kpi-cost-desc'] ? elementsById['kpi-cost-desc'].textContent : '',
        cacheRatioText: elementsById['kpi-cache-ratio'] ? elementsById['kpi-cache-ratio'].textContent : '',
        cacheDescText: elementsById['kpi-cache-desc'] ? elementsById['kpi-cache-desc'].textContent : '',
        inputTotalText: elementsById['kpi-max-savings'] ? elementsById['kpi-max-savings'].textContent : '',
        inputDescText: elementsById['kpi-savings-desc'] ? elementsById['kpi-savings-desc'].textContent : '',
        outputTotalText: elementsById['kpi-output-tokens'] ? elementsById['kpi-output-tokens'].textContent : '',
        outputDescText: elementsById['kpi-output-desc'] ? elementsById['kpi-output-desc'].textContent : '',
        toolbarTurnsText: elementsById['toolbar-turns-badge'] ? elementsById['toolbar-turns-badge'].textContent : '',
        toolbarCostText: elementsById['toolbar-cost-badge'] ? elementsById['toolbar-cost-badge'].textContent : '',
        turnsCountBadge: elementsById['session-turns-count-badge'] ? elementsById['session-turns-count-badge'].textContent : '',
        toolsTotalBadge: elementsById['session-tools-total-badge'] ? elementsById['session-tools-total-badge'].textContent : '',
        bqStatTurns: elementsById['bq-stat-total-turns'] ? elementsById['bq-stat-total-turns'].textContent : '',
        bqStatCost: elementsById['bq-stat-total-cost'] ? elementsById['bq-stat-total-cost'].textContent : '',
        bqCountBadge: elementsById['bq-table-count-badge'] ? elementsById['bq-table-count-badge'].textContent : ''
      },
      liveRows,
      csvExports
    };
    process.stdout.write(JSON.stringify(out));
  } catch (err) {
    process.stderr.write(String(err && err.stack ? err.stack : err));
    process.exit(1);
  }
})();
"""


class InMemoryOTelCollector:
    """In-memory OpenTelemetry MeterProvider boundary capturing histogram data points."""

    def __init__(self) -> None:
        self.instruments: list[tuple[str, str, str]] = []
        self.points: list[tuple[float, dict[str, Any]]] = []

    def get_meter(self, name: str):
        collector = self

        class _Histogram:
            def record(
                self, value: float, attributes: dict[str, Any] | None = None
            ) -> None:
                collector.points.append((float(value), dict(attributes or {})))

        class _Meter:
            def create_histogram(
                self, name: str, unit: str = "", description: str = ""
            ) -> _Histogram:
                collector.instruments.append((name, unit, description))
                return _Histogram()

        return _Meter()


class CapturingBigQueryBoundary:
    """
    Captures streaming `insert_rows_json` writes at the GCP SDK network boundary and
    evaluates the SDK/CLI/Web SQL queries directly over the captured BigQuery rows.
    """

    def __init__(self, default_project: str = "finops-e2e-proj") -> None:
        self.default_project = default_project
        self.datasets: dict[str, Any] = {}
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.table_schemas: dict[str, list[Any]] = {}
        self.views: dict[str, str] = {}
        self.view_ddls: dict[str, str] = {}
        self.executed_queries: list[str] = []
        self._lock = threading.Lock()

    def register_view(self, view_fqn: str, source_table_fqn: str) -> None:
        with self._lock:
            self.views[view_fqn] = source_table_fqn

    def make_module(
        self,
    ) -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
        boundary = self

        class NotFound(Exception):
            pass

        class SchemaField:
            def __init__(self, name: str, field_type: str = "STRING") -> None:
                self.name = name
                self.field_type = field_type

        class Dataset:
            def __init__(self, dataset_id: str) -> None:
                self.dataset_id = dataset_id
                self.location = "US"

        class _Table:
            def __init__(
                self, table_id: str, schema: list[SchemaField]
            ) -> None:
                self.table_id = table_id
                self.schema = list(schema)

        class ScalarQueryParameter:
            def __init__(self, name: str, param_type: str, value: Any) -> None:
                self.name = name
                self.param_type = param_type
                self.value = value

        class QueryJobConfig:
            def __init__(
                self,
                query_parameters: list[ScalarQueryParameter] | None = None,
            ) -> None:
                self.query_parameters = list(query_parameters or [])

        class _QueryJob:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._rows = rows

            def result(self) -> list[dict[str, Any]]:
                return list(self._rows)

        class Client:
            def __init__(self, project: str | None = None) -> None:
                self.project = project or boundary.default_project

            def create_dataset(
                self, dataset: Any, exists_ok: bool = False
            ) -> Any:
                ds_id = str(getattr(dataset, "dataset_id", dataset))
                with boundary._lock:
                    if ds_id in boundary.datasets and not exists_ok:
                        raise RuntimeError(
                            f"409 Already Exists: Dataset {ds_id}"
                        )
                    boundary.datasets[ds_id] = dataset
                return dataset

            def get_table(self, table: str) -> _Table:
                with boundary._lock:
                    if table in boundary.table_schemas:
                        return _Table(table, boundary.table_schemas[table])
                    if boundary.tables.get(table):
                        inferred = [
                            SchemaField(str(k))
                            for k in boundary.tables[table][0].keys()
                        ]
                        return _Table(table, inferred)
                raise NotFound(f"404 Not found: Table {table}")

            def insert_rows_json(
                self, table: str, rows: list[dict[str, Any]]
            ) -> list[Any]:
                with boundary._lock:
                    if table in boundary.table_schemas:
                        allowed_cols = {
                            sf.name for sf in boundary.table_schemas[table]
                        }
                        errors: list[dict[str, Any]] = []
                        valid_rows: list[dict[str, Any]] = []
                        for idx, row_obj in enumerate(rows):
                            bad_cols = [
                                k
                                for k in row_obj.keys()
                                if k not in allowed_cols
                            ]
                            if bad_cols:
                                errors.append(
                                    {
                                        "index": idx,
                                        "errors": [
                                            {
                                                "reason": "invalid",
                                                "location": bad_cols[0],
                                                "message": f"no such field: {bad_cols[0]}.",
                                            }
                                        ],
                                    }
                                )
                            else:
                                valid_rows.append(row_obj)
                        if valid_rows:
                            bucket = boundary.tables.setdefault(table, [])
                            bucket.extend(json.loads(json.dumps(valid_rows)))
                        return errors
                    bucket = boundary.tables.setdefault(table, [])
                    bucket.extend(json.loads(json.dumps(rows)))
                return []

            def query(
                self, sql: str, job_config: QueryJobConfig | None = None
            ) -> _QueryJob:
                rows = boundary.execute_sql(sql, job_config=job_config)
                return _QueryJob(rows)

        google_pkg = sys.modules.get("google") or types.ModuleType("google")
        cloud_pkg = sys.modules.get("google.cloud") or types.ModuleType(
            "google.cloud"
        )
        bq_mod = types.ModuleType("google.cloud.bigquery")
        bq_mod.Client = Client  # type: ignore[attr-defined]
        bq_mod.Dataset = Dataset  # type: ignore[attr-defined]
        bq_mod.SchemaField = SchemaField  # type: ignore[attr-defined]
        bq_mod.QueryJobConfig = QueryJobConfig  # type: ignore[attr-defined]
        bq_mod.ScalarQueryParameter = ScalarQueryParameter  # type: ignore[attr-defined]
        bq_mod.NotFound = NotFound  # type: ignore[attr-defined]
        cloud_pkg.bigquery = bq_mod  # type: ignore[attr-defined]
        google_pkg.cloud = cloud_pkg  # type: ignore[attr-defined]
        return google_pkg, cloud_pkg, bq_mod

    @staticmethod
    def _parse_ddl_columns(cols_body: str) -> list[Any]:
        fields = []
        for raw_line in cols_body.splitlines():
            clean_line = raw_line.strip().rstrip(",")
            if not clean_line:
                continue
            m_col = _DDL_COL_LINE_RE.match(clean_line)
            if m_col:
                col_name, col_type = m_col.group(1), m_col.group(2)
                fields.append(
                    types.SimpleNamespace(name=col_name, field_type=col_type)
                )
        return fields

    @staticmethod
    def _normalize_stored_row(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalizes either ADK `agent_events` JSON schema or flat BigQuerySink schema into canonical columns."""
        if "attributes" in raw or "content" in raw:
            attrs = (
                json.loads(raw["attributes"])
                if isinstance(raw.get("attributes"), str)
                else dict(raw.get("attributes") or {})
            )
            content = (
                json.loads(raw["content"])
                if isinstance(raw.get("content"), str)
                else dict(raw.get("content") or {})
            )
            um = attrs.get("usage_metadata") or {}
            content_usage_val = content.get("usage")
            content_usage: dict[str, Any] = (
                content_usage_val if isinstance(content_usage_val, dict) else {}
            )
            lat_raw = raw.get("latency_ms")
            if isinstance(lat_raw, str) and lat_raw.strip().startswith("{"):
                total_ms = int(json.loads(lat_raw).get("total_ms") or 0)
            elif isinstance(lat_raw, (int, float)):
                total_ms = int(lat_raw)
            else:
                total_ms = 0
            tool_use_tok = int(
                um.get("tool_use_prompt_token_count")
                or um.get("toolUsePromptTokenCount")
                or attrs.get("usage_tool_use_tokens")
                or 0
            )
            if content_usage.get("prompt") is not None:
                base_prompt_tok = int(content_usage.get("prompt") or 0)
            elif (
                um.get("prompt_token_count") is not None
                or um.get("promptTokens") is not None
            ):
                base_prompt_tok = int(
                    um.get("prompt_token_count") or um.get("promptTokens") or 0
                )
            else:
                base_prompt_tok = int(attrs.get("usage_prompt_tokens") or 0)
            prompt_tok = base_prompt_tok + tool_use_tok
            cached_tok = int(
                um.get("cached_content_token_count")
                or attrs.get("usage_cached_tokens")
                or 0
            )
            out_tok = int(
                content_usage.get("completion")
                or um.get("candidates_token_count")
                or attrs.get("usage_completion_tokens")
                or 0
            )
            thk_tok = int(
                um.get("thoughts_token_count")
                or attrs.get("usage_thinking_tokens")
                or 0
            )
            model_ver = str(
                attrs.get("model_version")
                or attrs.get("model")
                or content.get("model_version")
                or "gemini-3.8-flash"
            )
            agent_col = str(
                raw.get("agent") or attrs.get("sub_agent") or "agent"
            )
            return {
                "timestamp": str(raw.get("timestamp") or ""),
                "event_type": str(raw.get("event_type") or "LLM_RESPONSE"),
                "agent": agent_col,
                "sub_agent": str(attrs.get("sub_agent") or agent_col),
                "orchestration_flow": str(
                    attrs.get("orchestration_flow") or ""
                ),
                "session_id": str(raw.get("session_id") or ""),
                "invocation_id": str(raw.get("invocation_id") or ""),
                "model_version": model_ver,
                "model_name": model_ver,
                "base_prompt_tokens": base_prompt_tok,
                "usage_tool_use_tokens": tool_use_tok,
                "usage_prompt_tokens": prompt_tok,
                "usage_cached_tokens": cached_tok,
                "usage_completion_tokens": out_tok,
                "usage_thinking_tokens": thk_tok,
                "total_ms": total_ms,
                "latency_ms": raw.get("latency_ms"),
                "user_query": str(content.get("user_query") or ""),
                "agent_response": str(content.get("response") or ""),
                "content": raw.get("content") or "{}",
                "attributes": raw.get("attributes") or "{}",
                "raw": dict(raw),
            }

        # Flat BigQuerySink table format (e.g. table_id="2026_adk-events")
        inp = int(raw.get("input_tokens") or 0)
        cac = int(raw.get("cached_tokens") or 0)
        out = int(raw.get("output_tokens") or 0)
        thk = int(raw.get("thinking_tokens") or 0)
        prompt_tok = inp + cac
        raw_j_str = raw.get("raw_json") or "{}"
        try:
            raw_meta = (
                json.loads(raw_j_str)
                if isinstance(raw_j_str, str)
                else dict(raw_j_str)
            )
        except Exception:
            raw_meta = {}
        sub_agent = str(
            raw_meta.get("sub_agent") or raw.get("app_name") or "agent"
        )
        flow = str(raw_meta.get("orchestration_flow") or "")
        model_ver = str(raw.get("model_name") or "gemini-3.8-flash")
        synth_content = json.dumps(
            {
                "model_version": model_ver,
                "user_query": str(raw.get("user_query") or ""),
                "response": str(raw.get("agent_response") or ""),
            }
        )
        synth_attrs = json.dumps(
            {
                "model": model_ver,
                "model_version": model_ver,
                "sub_agent": sub_agent,
                "orchestration_flow": flow,
                "total_cost": float(raw.get("total_cost") or 0.0),
                "usage_prompt_tokens": prompt_tok,
                "usage_cached_tokens": cac,
                "usage_completion_tokens": out,
                "usage_thinking_tokens": thk,
                "tools_called": list(raw.get("tools_called") or []),
                "skills_active": list(raw.get("skills_active") or []),
                "usage_metadata": {
                    "prompt_token_count": prompt_tok,
                    "cached_content_token_count": cac,
                    "candidates_token_count": out,
                    "thoughts_token_count": thk,
                    "total_token_count": prompt_tok + out + thk,
                },
            }
        )
        return {
            "timestamp": str(raw.get("timestamp") or ""),
            "event_type": "LLM_RESPONSE",
            "agent": sub_agent,
            "sub_agent": sub_agent,
            "orchestration_flow": flow,
            "session_id": str(raw.get("session_id") or ""),
            "invocation_id": str(raw_meta.get("invocation_id") or ""),
            "model_version": model_ver,
            "model_name": model_ver,
            "usage_prompt_tokens": prompt_tok,
            "usage_cached_tokens": cac,
            "usage_completion_tokens": out,
            "usage_thinking_tokens": thk,
            "total_ms": int(raw_meta.get("latency_ms") or 0),
            "latency_ms": json.dumps(
                {"total_ms": int(raw_meta.get("latency_ms") or 0)}
            )
            if raw_meta.get("latency_ms")
            else None,
            "user_query": str(raw.get("user_query") or ""),
            "agent_response": str(raw.get("agent_response") or ""),
            "content": synth_content,
            "attributes": synth_attrs,
            "raw": dict(raw),
        }

    def execute_sql(
        self, sql: str, job_config: Any = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.executed_queries.append(sql)

            m_create_tbl = _CREATE_TABLE_DDL_RE.search(sql)
            if m_create_tbl:
                if_not_exists = bool(m_create_tbl.group(1))
                tbl_fqn = m_create_tbl.group(2)
                cols_body = m_create_tbl.group(3)
                parsed_fields = self._parse_ddl_columns(cols_body)
                if not if_not_exists or tbl_fqn not in self.table_schemas:
                    self.table_schemas[tbl_fqn] = parsed_fields
                self.tables.setdefault(tbl_fqn, [])
                return []

            m_create_view = _CREATE_VIEW_DDL_RE.search(sql)
            if m_create_view:
                or_replace = bool(m_create_view.group(1))
                if or_replace:
                    raise RuntimeError(
                        "Analytics views must use CREATE VIEW IF NOT EXISTS, never CREATE OR REPLACE VIEW."
                    )
                if_not_exists = bool(m_create_view.group(2))
                view_fqn = m_create_view.group(3)
                view_select_sql = m_create_view.group(4)
                m_src = _FROM_TARGET_RE.search(view_select_sql)
                if not m_src:
                    raise RuntimeError(
                        f"Missing FROM target in CREATE VIEW DDL: {sql}"
                    )
                src_tbl_fqn = m_src.group(1)
                if src_tbl_fqn not in self.tables:
                    raise LookupError(f"404 Not found: Table {src_tbl_fqn}")
                if src_tbl_fqn in self.table_schemas:
                    src_cols = {
                        sf.name.lower()
                        for sf in self.table_schemas[src_tbl_fqn]
                    }
                    if (
                        _WORD_ATTR_RE.search(view_select_sql)
                        and "attributes" not in src_cols
                    ):
                        raise RuntimeError(
                            f"400 Unrecognized name: attributes in {src_tbl_fqn}"
                        )
                if not if_not_exists or view_fqn not in self.views:
                    self.views[view_fqn] = src_tbl_fqn
                    self.view_ddls[view_fqn] = sql
                return []

            m_target = _FROM_TARGET_RE.search(sql)
            if not m_target:
                raise RuntimeError(
                    f"Missing backtick-quoted FROM target in SQL: {sql}"
                )
            target_fqn = m_target.group(1)
            is_view_target = target_fqn in self.views
            resolved_table = self.views.get(target_fqn, target_fqn)
            if resolved_table not in self.tables:
                raise LookupError(f"404 Not found: Table {target_fqn}")
            if not is_view_target and target_fqn in self.table_schemas:
                sql_no_lits = _SINGLE_QUOTED_LIT_RE.sub("''", sql)
                allowed_base_cols = {
                    sf.name.lower() for sf in self.table_schemas[target_fqn]
                }
                if (
                    "attributes" not in allowed_base_cols
                    and _WORD_ATTR_RE.search(sql_no_lits)
                ):
                    raise RuntimeError(
                        f"400 Unrecognized name: attributes in flat table {target_fqn}"
                    )
                if (
                    "usage_prompt_tokens" not in allowed_base_cols
                    and _WORD_USAGE_PROMPT_RE.search(sql_no_lits)
                    and not _AS_USAGE_PROMPT_RE.search(sql_no_lits)
                ):
                    raise RuntimeError(
                        f"400 Unrecognized name: usage_prompt_tokens in {target_fqn}"
                    )
                if (
                    "usage_tool_use_tokens" not in allowed_base_cols
                    and _WORD_USAGE_TOOL_RE.search(sql_no_lits)
                    and not _AS_USAGE_TOOL_RE.search(sql_no_lits)
                ):
                    raise RuntimeError(
                        f"400 Unrecognized name: usage_tool_use_tokens in {target_fqn}"
                    )
            raw_rows = [dict(r) for r in self.tables[resolved_table]]

        params = {
            p.name: p.value
            for p in getattr(job_config, "query_parameters", []) or []
        }
        norm_rows = [self._normalize_stored_row(r) for r in raw_rows]
        if is_view_target or "LLM_RESPONSE" in sql:
            llm_rows = [
                r
                for r in norm_rows
                if r["event_type"] in ("LLM_RESPONSE", "", None)
            ]
        else:
            llm_rows = norm_rows

        # 1. BigQuerySink.fetch_recent: SELECT * FROM `...` ORDER BY timestamp DESC LIMIT @limit
        if (
            _SELECT_STAR_RE.search(sql)
            and "WITH " not in sql.upper()
            and "WHERE session_id = @session_id" not in sql
        ):
            limit = int(params.get("limit", 50))
            if is_view_target:
                ordered_view = sorted(
                    llm_rows,
                    key=lambda r: str(r.get("timestamp") or ""),
                    reverse=True,
                )
                return [
                    {
                        "timestamp": r["timestamp"],
                        "event_type": r["event_type"],
                        "agent": r["agent"],
                        "session_id": r["session_id"],
                        "invocation_id": r["invocation_id"],
                        "model_version": r["model_version"],
                        "usage_prompt_tokens": r.get(
                            "base_prompt_tokens", r["usage_prompt_tokens"]
                        ),
                        "usage_tool_use_tokens": r.get(
                            "usage_tool_use_tokens", 0
                        ),
                        "usage_cached_tokens": r["usage_cached_tokens"],
                        "usage_completion_tokens": r["usage_completion_tokens"],
                        "usage_thinking_tokens": r["usage_thinking_tokens"],
                        "total_ms": r["total_ms"],
                        "latency_ms": r["latency_ms"],
                        "user_query": r["user_query"],
                        "agent_response": r["agent_response"],
                        "sub_agent": r["sub_agent"],
                        "orchestration_flow": r["orchestration_flow"],
                        "attributes": r["attributes"],
                        "content": r["content"],
                    }
                    for r in ordered_view[:limit]
                ]
            ordered = sorted(
                raw_rows,
                key=lambda r: str(r.get("timestamp") or ""),
                reverse=True,
            )
            return ordered[:limit]

        # 2. cli_profiler.profile_from_bigquery: WHERE session_id = @session_id ... ORDER BY timestamp ASC
        if (
            "WHERE session_id = @session_id" in sql
            and "ORDER BY timestamp ASC" in sql
        ):
            sid = str(params.get("session_id") or "")
            source_pool = llm_rows if "LLM_RESPONSE" in sql else norm_rows
            matched = [r for r in source_pool if r["session_id"] == sid]
            matched.sort(key=lambda r: r["timestamp"])
            if "AS prompt_tokens" not in sql and (
                "content" in sql or "SELECT *" in sql.upper()
            ):
                return [dict(r["raw"]) for r in matched]
            return [
                {
                    "timestamp": r["timestamp"],
                    "event_type": r["event_type"],
                    "agent": r["agent"],
                    "session_id": r["session_id"],
                    "invocation_id": r["invocation_id"],
                    "sub_agent": r["sub_agent"],
                    "orchestration_flow": r["orchestration_flow"],
                    "user_query": r["user_query"],
                    "prompt_tokens": r["usage_prompt_tokens"],
                    "cached_tokens": r["usage_cached_tokens"],
                    "output_tokens": r["usage_completion_tokens"],
                    "thinking_tokens": r["usage_thinking_tokens"],
                    "latency_ms": r["total_ms"],
                    "model_name": r["model_name"],
                    "content": r["content"],
                    "attributes": r["attributes"],
                }
                for r in matched
            ]

        # Apply @app and @q filters for /api/bq/logs queries
        if "@app" in sql or "@q" in sql:
            app_f = str(params.get("app") or "all")
            q_f = str(params.get("q") or "").lower()
            filtered = []
            for r in norm_rows:
                if (
                    app_f != "all"
                    and r["agent"] != app_f
                    and r["sub_agent"] != app_f
                ):
                    continue
                if q_f:
                    haystack = f"{r['session_id']} {r['content']} {r['attributes']}".lower()
                    if q_f not in haystack:
                        continue
                filtered.append(r)

            if "COUNT(*) AS total" in sql:
                return [{"total": len(filtered)}]

            m_lim = _LIMIT_OFFSET_RE.search(sql)
            lim = int(m_lim.group(1)) if m_lim else 50
            off = int(m_lim.group(2)) if m_lim else 0
            filtered.sort(key=lambda r: r["timestamp"], reverse=True)
            return filtered[off : off + lim]

        # 3. cli_finops totals: GROUP BY model_name with fleet_unique_sessions & total_prompt_tokens
        if "GROUP BY model_name" in sql and "total_prompt_tokens" in sql:
            fleet_u = len(
                {r["session_id"] for r in llm_rows if r["session_id"]}
            )
            by_model: dict[str, list[dict[str, Any]]] = {}
            for r in llm_rows:
                by_model.setdefault(r["model_name"], []).append(r)
            return [
                {
                    "model_name": m,
                    "total_turns": len(grp),
                    "total_sessions": len(
                        {x["session_id"] for x in grp if x["session_id"]}
                    ),
                    "fleet_unique_sessions": fleet_u,
                    "total_prompt_tokens": sum(
                        x["usage_prompt_tokens"] for x in grp
                    ),
                    "total_cached_tokens": sum(
                        x["usage_cached_tokens"] for x in grp
                    ),
                    "total_output_tokens": sum(
                        x["usage_completion_tokens"] for x in grp
                    ),
                    "total_thinking_tokens": sum(
                        x["usage_thinking_tokens"] for x in grp
                    ),
                    "total_latency_ms": sum(x["total_ms"] for x in grp),
                }
                for m, grp in by_model.items()
            ]

        # 4. cli_finops top agents: GROUP BY agent_name (or GROUP BY agent_name, model_name)
        if "GROUP BY agent_name" in sql:
            if "GROUP BY agent_name, model_name" in sql:
                by_ag_model: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for r in llm_rows:
                    by_ag_model.setdefault(
                        (r["agent"], r["model_name"]), []
                    ).append(r)
                out_ag = [
                    {
                        "agent_name": ag,
                        "model_name": mod_name,
                        "turns": len(grp),
                        "prompt_tok": sum(
                            x["usage_prompt_tokens"] for x in grp
                        ),
                        "cached_tok": sum(
                            x["usage_cached_tokens"] for x in grp
                        ),
                        "out_tok": sum(
                            x["usage_completion_tokens"] for x in grp
                        ),
                        "thk_tok": sum(x["usage_thinking_tokens"] for x in grp),
                    }
                    for (ag, mod_name), grp in by_ag_model.items()
                ]
            else:
                by_ag: dict[str, list[dict[str, Any]]] = {}
                for r in llm_rows:
                    by_ag.setdefault(r["agent"], []).append(r)
                out_ag = [
                    {
                        "agent_name": ag,
                        "model_name": grp[0]["model_name"],
                        "turns": len(grp),
                        "prompt_tok": sum(
                            x["usage_prompt_tokens"] for x in grp
                        ),
                        "cached_tok": sum(
                            x["usage_cached_tokens"] for x in grp
                        ),
                        "out_tok": sum(
                            x["usage_completion_tokens"] for x in grp
                        ),
                        "thk_tok": sum(x["usage_thinking_tokens"] for x in grp),
                    }
                    for ag, grp in by_ag.items()
                ]
            out_ag.sort(key=lambda x: x["prompt_tok"], reverse=True)
            m_lim = _LIMIT_ONLY_RE.search(sql)
            return out_ag[: int(m_lim.group(1))] if m_lim else out_ag

        # 5. cli_web /api/bq/stats: GROUP BY model_version
        if "GROUP BY model_version" in sql:
            fleet_u = len(
                {r["session_id"] for r in llm_rows if r["session_id"]}
            )
            by_mv: dict[str, list[dict[str, Any]]] = {}
            for r in llm_rows:
                by_mv.setdefault(r["model_version"], []).append(r)
            return [
                {
                    "model_version": mv,
                    "total_turns": len(grp),
                    "total_input": sum(x["usage_prompt_tokens"] for x in grp),
                    "total_cached": sum(x["usage_cached_tokens"] for x in grp),
                    "total_output": sum(
                        x["usage_completion_tokens"] for x in grp
                    ),
                    "total_thinking": sum(
                        x["usage_thinking_tokens"] for x in grp
                    ),
                    "unique_sessions": len(
                        {x["session_id"] for x in grp if x["session_id"]}
                    ),
                    "fleet_unique_sessions": fleet_u,
                }
                for mv, grp in by_mv.items()
            ]

        # 6. cli_web /api/finops/summary: GROUP BY model_name, app_name or GROUP BY date_label, model_name
        if "GROUP BY model_name, app_name" in sql:
            by_ma: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for r in norm_rows:
                by_ma.setdefault((r["model_name"], r["agent"]), []).append(r)
            return [
                {
                    "model_name": m,
                    "app_name": a,
                    "turns": len(grp),
                    "input_tokens": sum(
                        max(
                            0,
                            x["usage_prompt_tokens"] - x["usage_cached_tokens"],
                        )
                        for x in grp
                    ),
                    "cached_tokens": sum(x["usage_cached_tokens"] for x in grp),
                    "output_tokens": sum(
                        x["usage_completion_tokens"] for x in grp
                    ),
                    "thinking_tokens": sum(
                        x["usage_thinking_tokens"] for x in grp
                    ),
                }
                for (m, a), grp in by_ma.items()
            ]

        if "GROUP BY date_label, model_name" in sql:
            by_dm: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for r in norm_rows:
                dl = (
                    r["timestamp"][:10]
                    if len(r["timestamp"]) >= 10
                    else "2026-11-04"
                )
                by_dm.setdefault((dl, r["model_name"]), []).append(r)
            return [
                {
                    "date_label": d,
                    "model_name": m,
                    "turns": len(grp),
                    "input_tokens": sum(
                        max(
                            0,
                            x["usage_prompt_tokens"] - x["usage_cached_tokens"],
                        )
                        for x in grp
                    ),
                    "cached_tokens": sum(x["usage_cached_tokens"] for x in grp),
                    "output_tokens": sum(
                        x["usage_completion_tokens"] for x in grp
                    ),
                    "thinking_tokens": sum(
                        x["usage_thinking_tokens"] for x in grp
                    ),
                    "daily_tokens": sum(
                        x["usage_prompt_tokens"]
                        + x["usage_completion_tokens"]
                        + x["usage_thinking_tokens"]
                        for x in grp
                    ),
                }
                for (d, m), grp in sorted(by_dm.items())
            ]

        return norm_rows


def _http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    data = (
        json.dumps(json_body).encode("utf-8") if json_body is not None else None
    )
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        return err.code, parsed


def _run_node_frontend_vm(
    base_url: str,
    session_id: str = "latest",
    enable_chart: bool = True,
    run_bq_csv: bool = False,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", encoding="utf-8", delete=False
    ) as hf:
        hf.write(NODE_APP_HARNESS_JS)
        harness_path = hf.name
    try:
        env = dict(os.environ)
        env["E2E_BASE_URL"] = base_url
        env["E2E_INDEX_HTML"] = os.path.join(
            REPO_ROOT, "adk_tokenomics_plugin", "web", "index.html"
        )
        env["E2E_APP_JS"] = os.path.join(
            REPO_ROOT, "adk_tokenomics_plugin", "web", "app.js"
        )
        env["E2E_ENABLE_CHART"] = "1" if enable_chart else "0"
        env["E2E_RUN_BQ_CSV"] = "1" if run_bq_csv else "0"
        env["E2E_SESSION_ID"] = session_id
        node_bin = shutil.which("node")
        if not node_bin:
            raise unittest.SkipTest("Node.js runtime not available on PATH")
        proc = subprocess.run(
            [node_bin, harness_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"Node VM harness failed (rc={proc.returncode}): {proc.stderr}"
            )
        return json.loads(proc.stdout)
    finally:
        if os.path.exists(harness_path):
            os.remove(harness_path)


def _simulate_multi_agent_session_lifecycle(traces_dir: str) -> dict[str, Any]:
    """
    Executes the multi-agent ADK lifecycle (root + parallel researchers with load_skill +
    Claude Opus 100% cached prefix turn + @track_tokens sync & async helpers + OTel collector),
    rotates to Session 2, and simulates Process 2 resuming Session 1.
    """
    live_path = os.path.join(traces_dir, "live_adk_session.json")
    otel_collector = InMemoryOTelCollector()

    fake_otel = types.ModuleType("opentelemetry")
    fake_metrics = types.ModuleType("opentelemetry.metrics")
    fake_metrics.get_meter = otel_collector.get_meter  # type: ignore[attr-defined]
    fake_otel.metrics = fake_metrics  # type: ignore[attr-defined]

    with patch.dict(
        sys.modules,
        {"opentelemetry": fake_otel, "opentelemetry.metrics": fake_metrics},
    ):
        tower_proc1 = TokenControlTower(mode="dev", sink="memory")
        plugin_proc1 = tower_proc1.create_adk_plugin(
            app_name="enterprise_finops_concierge",
            auto_export_path=live_path,
        )

        # Agent hierarchy for runtime orchestration_flow extraction
        parallel_parent = type(
            "ParallelAgent",
            (),
            {"name": "research_fanout", "parent_agent": None},
        )()
        root_agent = type(
            "LlmAgent",
            (),
            {
                "name": "root_concierge",
                "parent_agent": None,
                "sub_agents": [parallel_parent],
                "tools": [],
            },
        )()
        res_a_agent = type(
            "LlmAgent",
            (),
            {
                "name": "parallel_researcher_a",
                "parent_agent": parallel_parent,
                "tools": [],
            },
        )()
        res_b_agent = type(
            "LlmAgent",
            (),
            {
                "name": "parallel_researcher_b",
                "parent_agent": parallel_parent,
                "tools": [],
            },
        )()
        critic_seq = type(
            "SequentialAgent",
            (),
            {"name": "critic_pipeline", "parent_agent": None},
        )()
        critic_agent = type(
            "LlmAgent",
            (),
            {"name": "claude_critic", "parent_agent": critic_seq, "tools": []},
        )()

        user_msg_1 = type(
            "Content",
            (),
            {
                "role": "user",
                "parts": [
                    type(
                        "Part", (), {"text": "Audit Q4 multi-cloud AI spend"}
                    )()
                ],
            },
        )()

        # Turn 1: root_concierge on gemini-4-argon (fresh=6000, cached=4000, out=1000, think=500) -> cost=$0.0274, savings=$0.0076
        ctx_root = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_alpha",
                "invocation_id": "inv_alpha_1",
                "agent_name": "root_concierge",
                "agent": root_agent,
            },
        )()
        plugin_proc1.before_model_callback(
            callback_context=ctx_root,
            llm_request=type(
                "LlmRequest",
                (),
                {
                    "model": "publishers/google/models/gemini-4-argon",
                    "contents": [user_msg_1],
                },
            )(),
        )
        plugin_proc1.after_model_callback(
            callback_context=ctx_root,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 10000,
                        "cached_content_token_count": 4000,
                        "candidates_token_count": 1000,
                        "thoughts_token_count": 500,
                    },
                    "model_version": "publishers/google/models/gemini-4-argon",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "Dispatching parallel researchers.",
                                                    "function_call": None,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )

        # Turn 2 & Turn 3: Interleaved parallel_researcher_a & parallel_researcher_b on gemini-3.8-flash with load_skill tool calls
        ctx_pa = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_alpha",
                "invocation_id": "inv_alpha_1",
                "agent_name": "parallel_researcher_a",
                "agent": res_a_agent,
            },
        )()
        ctx_pb = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_alpha",
                "invocation_id": "inv_alpha_1",
                "agent_name": "parallel_researcher_b",
                "agent": res_b_agent,
            },
        )()
        req_flash = type(
            "LlmRequest",
            (),
            {
                "model": "publishers/google/models/gemini-3.8-flash",
                "contents": [user_msg_1],
            },
        )()
        plugin_proc1.before_model_callback(
            callback_context=ctx_pa, llm_request=req_flash
        )
        plugin_proc1.before_model_callback(
            callback_context=ctx_pb, llm_request=req_flash
        )

        fc_skill_a = type(
            "FunctionCall",
            (),
            {"name": "load_skill", "args": {"skill_name": "sec-filings"}},
        )()
        fc_skill_b = type(
            "FunctionCall",
            (),
            {"name": "load_skill", "args": {"skill_name": "cloud-billing"}},
        )()

        # Researcher A: fresh=4000, cached=4000, out=800, think=0 -> cost=$0.0063, savings=$0.0027
        plugin_proc1.after_model_callback(
            callback_context=ctx_pa,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 8000,
                        "cached_content_token_count": 4000,
                        "candidates_token_count": 800,
                        "thoughts_token_count": 0,
                    },
                    "model_version": "publishers/google/models/gemini-3.8-flash",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "SEC analysis ready.",
                                                    "function_call": fc_skill_a,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )
        # Researcher B: fresh=4000, cached=8000, out=1200, think=400 -> cost=$0.0096, savings=$0.0054
        plugin_proc1.after_model_callback(
            callback_context=ctx_pb,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 12000,
                        "cached_content_token_count": 8000,
                        "candidates_token_count": 1200,
                        "thoughts_token_count": 400,
                    },
                    "model_version": "publishers/google/models/gemini-3.8-flash",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "Billing breakdown ready.",
                                                    "function_call": fc_skill_b,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )
        load_skill_tool = type("Tool", (), {"name": "load_skill"})()
        plugin_proc1.after_tool_callback(
            tool=load_skill_tool,
            tool_args={"skill_name": "sec-filings"},
            tool_context=ctx_pa,
        )
        plugin_proc1.after_tool_callback(
            tool=load_skill_tool,
            tool_args={"skill_name": "cloud-billing"},
            tool_context=ctx_pb,
        )

        # Turn 4: claude_critic on claude-opus-4 with 100% cached prefix (prompt=10000, cached=10000 => fresh=0) + thinking=600, out=400
        # cost=$0.0900, savings=$0.1350
        ctx_critic = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_alpha",
                "invocation_id": "inv_alpha_2",
                "agent_name": "claude_critic",
                "agent": critic_agent,
            },
        )()
        plugin_proc1.before_model_callback(
            callback_context=ctx_critic,
            llm_request=type(
                "LlmRequest",
                (),
                {"model": "claude-opus-4", "contents": [user_msg_1]},
            )(),
        )
        plugin_proc1.after_model_callback(
            callback_context=ctx_critic,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_tokens": 10000,
                        "cache_read_input_tokens": 10000,
                        "completion_tokens": 400,
                        "reasoning_tokens": 600,
                    },
                    "model_version": "claude-opus-4",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "Audit verified with zero discrepancies.",
                                                    "function_call": None,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )

        # @track_tokens sync & async helper invocations on a dedicated helper tracker (and verify OTel emission)
        helper_tracker = TokenTracker(sink=InMemorySink())

        @track_tokens(
            app_name="sync_summarizer",
            model_name="gpt-5-mini",
            tracker=helper_tracker,
        )
        def summarize_sync(text: str):
            return (
                "Sync digest",
                {
                    "input_tokens": 2000,
                    "cached_tokens": 1000,
                    "output_tokens": 500,
                    "thinking_tokens": 0,
                },
            )

        @track_tokens(
            app_name="async_verifier",
            model_name="publishers/google/models/gemini-3.8-flash",
            tracker=helper_tracker,
        )
        async def verify_async(text: str):
            await asyncio.sleep(0.005)
            return (
                "Async verified",
                {
                    "prompt_tokens": 3000,
                    "cached_tokens": 2000,
                    "completion_tokens": 200,
                    "reasoning_tokens": 100,
                },
            )

        sync_out = summarize_sync("digest input")
        async_out = asyncio.run(verify_async("verify input"))

        # Rotate to Session 2 (sess_e2e_beta) -> automatically archives sess_e2e_alpha.json on disk!
        ctx_sess2 = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_beta",
                "invocation_id": "inv_beta_1",
                "agent_name": "root_concierge",
                "agent": root_agent,
            },
        )()
        user_msg_2 = type(
            "Content",
            (),
            {
                "role": "user",
                "parts": [
                    type("Part", (), {"text": "Quick status check on beta"})()
                ],
            },
        )()
        plugin_proc1.before_model_callback(
            callback_context=ctx_sess2,
            llm_request=type(
                "LlmRequest",
                (),
                {
                    "model": "publishers/google/models/gemini-3.8-flash",
                    "contents": [user_msg_2],
                },
            )(),
        )
        plugin_proc1.after_model_callback(
            callback_context=ctx_sess2,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 2000,
                        "cached_content_token_count": 1000,
                        "candidates_token_count": 200,
                        "thoughts_token_count": 0,
                    },
                    "model_version": "publishers/google/models/gemini-3.8-flash",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "Beta healthy.",
                                                    "function_call": None,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )
        plugin_proc1.finalize_session()

        # Simulate Process 2 restarting and resuming Session 1 (sess_e2e_alpha) with Turn 5:
        # root_concierge on gemini-4-argon: fresh=4000, cached=10000, out=600, think=400 -> cost=$0.0190, savings=$0.0190
        tower_proc2 = TokenControlTower(mode="dev", sink="memory")
        plugin_proc2 = tower_proc2.create_adk_plugin(
            app_name="enterprise_finops_concierge",
            auto_export_path=live_path,
        )
        ctx_resume = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_e2e_alpha",
                "invocation_id": "inv_alpha_3",
                "agent_name": "root_concierge",
                "agent": root_agent,
            },
        )()
        user_msg_3 = type(
            "Content",
            (),
            {
                "role": "user",
                "parts": [
                    type(
                        "Part",
                        (),
                        {"text": "Generate board-ready executive memo"},
                    )()
                ],
            },
        )()
        plugin_proc2.before_model_callback(
            callback_context=ctx_resume,
            llm_request=type(
                "LlmRequest",
                (),
                {
                    "model": "publishers/google/models/gemini-4-argon",
                    "contents": [user_msg_3],
                },
            )(),
        )
        plugin_proc2.after_model_callback(
            callback_context=ctx_resume,
            llm_response=type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 14000,
                        "cached_content_token_count": 10000,
                        "candidates_token_count": 600,
                        "thoughts_token_count": 400,
                    },
                    "model_version": "publishers/google/models/gemini-4-argon",
                    "candidates": [
                        type(
                            "Cand",
                            (),
                            {
                                "content": type(
                                    "Ct",
                                    (),
                                    {
                                        "parts": [
                                            type(
                                                "P",
                                                (),
                                                {
                                                    "text": "Executive memo finalized.",
                                                    "function_call": None,
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            },
                        )()
                    ],
                },
            )(),
        )

        proc2_live_summary = tower_proc2.get_summary()
        with open(live_path, encoding="utf-8") as lf:
            live_before_finalize = json.load(lf)
        alpha_archive_path = plugin_proc2.finalize_session()

    return {
        "tower_proc1": tower_proc1,
        "tower_proc2": tower_proc2,
        "proc2_live_summary": proc2_live_summary,
        "live_before_finalize": live_before_finalize,
        "alpha_archive_path": alpha_archive_path,
        "beta_archive_path": os.path.join(traces_dir, "sess_e2e_beta.json"),
        "helper_tracker": helper_tracker,
        "sync_out": sync_out,
        "async_out": async_out,
        "otel_collector": otel_collector,
    }


class TestComprehensiveE2E(unittest.TestCase):
    """Full end-to-end integration & regression test suite across all surfaces."""

    def test_scenario_1_multi_agent_lifecycle_rotation_hydration_and_otel(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = _simulate_multi_agent_session_lifecycle(tmp_dir)

            # 1. Verify Session 1 & Session 2 archive files both exist after rotation and finalization
            self.assertIsNotNone(res["alpha_archive_path"])
            self.assertTrue(os.path.exists(res["alpha_archive_path"]))
            self.assertTrue(os.path.exists(res["beta_archive_path"]))

            # 2. Verify P2-2 isolation: Process 2 in-memory summary has ONLY its 1 live turn ($0.0190),
            #    while live_adk_session.json (pre-finalize) and sess_e2e_alpha.json have ALL 5 merged turns ($0.1523)!
            proc2_sum = res["proc2_live_summary"]
            self.assertEqual(proc2_sum["total_turns"], 1)
            self.assertEqual(len(res["tower_proc2"].tracker.turns_history), 1)
            self.assertEqual(proc2_sum["input_tokens"], 4000)
            self.assertEqual(proc2_sum["cached_tokens"], 10000)
            self.assertEqual(proc2_sum["output_tokens"], 600)
            self.assertEqual(proc2_sum["thinking_tokens"], 400)
            self.assertAlmostEqual(proc2_sum["total_cost"], 0.019000, places=6)

            self.assertEqual(len(res["live_before_finalize"]["turns"]), 5)
            self.assertEqual(
                res["live_before_finalize"]["summary"]["total_turns"], 5
            )

            with open(res["alpha_archive_path"], encoding="utf-8") as af:
                alpha_doc = json.load(af)
            self.assertEqual(len(alpha_doc["turns"]), 5)
            self.assertEqual(alpha_doc["summary"]["total_turns"], 5)
            self.assertEqual(alpha_doc["summary"]["input_tokens"], 18000)
            self.assertEqual(alpha_doc["summary"]["cached_tokens"], 36000)
            self.assertEqual(alpha_doc["summary"]["output_tokens"], 4000)
            self.assertEqual(alpha_doc["summary"]["thinking_tokens"], 1900)
            self.assertAlmostEqual(
                alpha_doc["summary"]["total_cost"], 0.152300, places=6
            )

            # 3. Verify parallel sub-agent skill attribution & orchestration flows & 100% cached Claude turn
            turns = alpha_doc["turns"]
            self.assertEqual(
                [t["sub_agent"] for t in turns],
                [
                    "root_concierge",
                    "parallel_researcher_a",
                    "parallel_researcher_b",
                    "claude_critic",
                    "root_concierge",
                ],
            )
            self.assertEqual(turns[0]["orchestration_flow"], "Root")
            self.assertEqual(turns[1]["orchestration_flow"], "Parallel")
            self.assertEqual(turns[1]["skills_active"], ["sec-filings"])
            self.assertEqual(turns[2]["orchestration_flow"], "Parallel")
            self.assertEqual(turns[2]["skills_active"], ["cloud-billing"])
            self.assertEqual(turns[3]["orchestration_flow"], "Sequential")
            self.assertEqual(turns[3]["input_tokens"], 0)
            self.assertEqual(turns[3]["cached_tokens"], 10000)
            self.assertEqual(turns[3]["thinking_tokens"], 600)

            # 4. Verify @track_tokens sync (gpt-5-mini tier guard!) & async helpers
            helper_turns = res["helper_tracker"].turns_history
            self.assertEqual(res["sync_out"][0], "Sync digest")
            self.assertEqual(res["async_out"][0], "Async verified")
            self.assertEqual(len(helper_turns), 2)
            # gpt-5-mini: 2000 * 0.15/1M + 1000 * 0.075/1M + 500 * 0.60/1M = 0.0003 + 0.000075 + 0.0003 = 0.000675
            self.assertAlmostEqual(
                helper_turns[0]["total_cost"], 0.000675, places=8
            )
            self.assertEqual(helper_turns[1]["input_tokens"], 1000)
            self.assertEqual(helper_turns[1]["cached_tokens"], 2000)
            self.assertEqual(helper_turns[1]["output_tokens"], 200)
            self.assertEqual(helper_turns[1]["thinking_tokens"], 100)

            # 5. Verify OpenTelemetry gen_ai.client.cost.usd actual & cache_savings histograms
            otel: InMemoryOTelCollector = res["otel_collector"]
            self.assertTrue(
                any(
                    inst[0] == "gen_ai.client.cost.usd" and inst[1] == "USD"
                    for inst in otel.instruments
                )
            )
            actual_pts = [
                v
                for v, a in otel.points
                if a.get("gen_ai.cost.type") == "actual"
            ]
            savings_pts = [
                v
                for v, a in otel.points
                if a.get("gen_ai.cost.type") == "cache_savings"
            ]
            # 5 alpha turns + 1 beta turn + 2 helper turns = 8 turns, all with cached_tokens > 0
            self.assertEqual(len(actual_pts), 8)
            self.assertEqual(len(savings_pts), 8)

    def test_scenario_2_five_surface_four_bucket_math_parity(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            traces_dir = os.path.join(tmp_dir, "traces")
            xdg_dir = os.path.join(tmp_dir, "xdg")
            os.makedirs(traces_dir, exist_ok=True)
            os.makedirs(xdg_dir, exist_ok=True)

            res = _simulate_multi_agent_session_lifecycle(traces_dir)
            alpha_path = res["alpha_archive_path"]

            with open(alpha_path, encoding="utf-8") as f:
                alpha_payload = json.load(f)

            # Surface 1: SDK (PricingEngine + FinOpsAggregator + TokenTracker exported summary)
            pricing = PricingEngine()
            sdk_turns = alpha_payload["turns"]
            sdk_cost = sum(
                pricing.calculate_turn_cost(
                    t["model_name"],
                    t["input_tokens"],
                    t["cached_tokens"],
                    t["output_tokens"],
                    t["thinking_tokens"],
                )["total_cost"]
                for t in sdk_turns
            )
            sdk_savings = sum(
                pricing.calculate_turn_cost(
                    t["model_name"],
                    t["input_tokens"],
                    t["cached_tokens"],
                    t["output_tokens"],
                    t["thinking_tokens"],
                )["savings_dollars"]
                for t in sdk_turns
            )
            sdk_fresh = sum(t["input_tokens"] for t in sdk_turns)
            sdk_cached = sum(t["cached_tokens"] for t in sdk_turns)
            sdk_output = sum(t["output_tokens"] for t in sdk_turns)
            sdk_thinking = sum(t["thinking_tokens"] for t in sdk_turns)

            agg = FinOpsAggregator.aggregate_records(sdk_turns)
            self.assertAlmostEqual(
                agg["kpis"]["total_spend"], sdk_cost, places=5
            )
            self.assertEqual(agg["kpis"]["total_input"], sdk_fresh)
            self.assertEqual(agg["kpis"]["total_cached"], sdk_cached)
            self.assertEqual(agg["kpis"]["total_output"], sdk_output)
            self.assertEqual(agg["kpis"]["total_thinking"], sdk_thinking)

            clean_env = dict(os.environ)
            clean_env["XDG_CONFIG_HOME"] = xdg_dir
            clean_env.pop("TOKENOMICS_MODELS_CONFIG_PATH", None)
            clean_env["PYTHONPATH"] = REPO_ROOT

            # Surface 2: CLI `profile --format json` via real subprocess
            prof_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin.cli",
                    "profile",
                    "--traces",
                    alpha_path,
                    "--format",
                    "json",
                ],
                env=clean_env,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(prof_proc.returncode, 0, msg=prof_proc.stderr)
            prof_json = json.loads(prof_proc.stdout)
            prof_bd = prof_json["sub_agent_breakdown"]
            prof_cost = sum(v["cost"] for v in prof_bd.values())
            prof_fresh = sum(v["fresh"] for v in prof_bd.values())
            prof_cached = sum(v["cached"] for v in prof_bd.values())
            prof_output = sum(v["output"] for v in prof_bd.values())
            prof_thinking = sum(v["thinking"] for v in prof_bd.values())
            prof_savings = sum(
                pricing.calculate_turn_cost(
                    t["model_name"],
                    t["input_tokens"],
                    t["cached_tokens"],
                    t["output_tokens"],
                    t["thinking_tokens"],
                )["savings_dollars"]
                for t in prof_json["turns"]
            )

            # Surface 3: CLI `assert --format json` via real subprocess
            assert_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin.cli",
                    "assert",
                    "--traces",
                    alpha_path,
                    "--max-cost-per-turn",
                    "0.05",
                    "--min-cache-rate",
                    "0.60",
                    "--max-tokens-per-turn",
                    "15000",
                    "--max-latency-ms",
                    "5000",
                    "--format",
                    "json",
                ],
                env=clean_env,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                assert_proc.returncode,
                0,
                msg=assert_proc.stdout + assert_proc.stderr,
            )
            assert_json = json.loads(assert_proc.stdout)
            self.assertTrue(assert_json["passed"])
            self.assertEqual(assert_json["total_turns"], 5)
            assert_total_cost = (
                assert_json["avg_cost_per_turn"] * assert_json["total_turns"]
            )

            # Surface 4 & Surface 5: Live Web `/api/metrics` + Frontend `app.js` DOM KPIs in Node VM
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": xdg_dir,
                    "TOKENOMICS_MODELS_CONFIG_PATH": "",
                },
                clear=False,
            ):
                TokenomicsWebHandler.set_server_context(
                    project_id="",
                    traces_path=traces_dir,
                    bound_host="127.0.0.1",
                )
                httpd = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), TokenomicsWebHandler
                )
                port = httpd.server_address[1]
                srv_thread = threading.Thread(
                    target=httpd.serve_forever, daemon=True
                )
                srv_thread.start()
                try:
                    base_url = f"http://127.0.0.1:{port}"
                    status, web_metrics = _http_request(
                        f"{base_url}/api/metrics?session_id=sess_e2e_alpha"
                    )
                    self.assertEqual(status, 200)

                    fe_dom = _run_node_frontend_vm(
                        base_url, session_id="sess_e2e_alpha", enable_chart=True
                    )
                    self.assertEqual(fe_dom["runtimeErrors"], [])
                    self.assertEqual(fe_dom["chartCount"], 2)
                finally:
                    httpd.shutdown()
                    httpd.server_close()

            web_cost = float(web_metrics["total_cost"])
            web_savings = float(web_metrics["savings_dollars"])
            web_fresh = int(web_metrics["prompt_tokens"]) - int(
                web_metrics["cached_tokens"]
            )
            web_cached = int(web_metrics["cached_tokens"])
            web_output = int(web_metrics["output_tokens"])
            web_thinking = int(web_metrics["thinking_tokens"])

            fe_rows = fe_dom["liveRows"]
            fe_cost = sum(r["cost"] for r in fe_rows)
            fe_fresh = sum(r["fresh"] for r in fe_rows)
            fe_cached = sum(r["cached"] for r in fe_rows)
            fe_output = sum(r["output"] for r in fe_rows)
            fe_thinking = sum(r["thinking"] for r in fe_rows)

            # Parse rendered KPI card text from app.js DOM
            kpi_cost_num = float(fe_dom["kpis"]["totalCostText"].lstrip("$"))
            m_sav = _SAVED_DOLLARS_RE.search(fe_dom["kpis"]["cacheDescText"])
            self.assertIsNotNone(m_sav, msg=fe_dom["kpis"]["cacheDescText"])
            kpi_savings_num = float(m_sav.group(1)) if m_sav else -1.0

            # Assert 6-decimal-place parity across ALL FIVE surfaces!
            expected_cost = 0.152300
            expected_savings = 0.169700
            for surface_name, cost_val in (
                ("SDK", sdk_cost),
                (
                    "ExportSummary",
                    float(alpha_payload["summary"]["total_cost"]),
                ),
                ("CLI_profile", prof_cost),
                ("CLI_assert", assert_total_cost),
                ("Web_API", web_cost),
                ("Frontend_DOM_Table", fe_cost),
                ("Frontend_DOM_KPI", kpi_cost_num),
            ):
                self.assertAlmostEqual(
                    cost_val,
                    expected_cost,
                    places=6,
                    msg=f"Cost mismatch on {surface_name}",
                )

            for surface_name, sav_val in (
                ("SDK", sdk_savings),
                ("CLI_profile", prof_savings),
                ("Web_API", web_savings),
                ("Frontend_DOM_KPI", kpi_savings_num),
            ):
                self.assertAlmostEqual(
                    sav_val,
                    expected_savings,
                    places=6,
                    msg=f"Savings mismatch on {surface_name}",
                )

            for surface_name, bucket_tuple in (
                ("SDK", (sdk_fresh, sdk_cached, sdk_output, sdk_thinking)),
                (
                    "CLI_profile",
                    (prof_fresh, prof_cached, prof_output, prof_thinking),
                ),
                ("Web_API", (web_fresh, web_cached, web_output, web_thinking)),
                ("Frontend_DOM", (fe_fresh, fe_cached, fe_output, fe_thinking)),
            ):
                self.assertEqual(
                    bucket_tuple,
                    (18000, 36000, 4000, 1900),
                    msg=f"4-bucket mismatch on {surface_name}",
                )

            self.assertEqual(
                fe_dom["kpis"]["inputDescText"],
                "Fresh: 18,000 | Cached: 36,000",
            )
            self.assertIn(
                "Visible: 4,000 | Thoughts: 1,900",
                fe_dom["kpis"]["outputDescText"],
            )

    def test_scenario_3_web_rate_card_crud_security_matrix_and_offline_resilience(
        self,
    ):
        pkg_models_path = os.path.join(
            REPO_ROOT, "adk_tokenomics_plugin", "models_config.json"
        )
        with open(pkg_models_path, "rb") as pf:
            sha256_before = hashlib.sha256(pf.read()).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            traces_dir = os.path.join(tmp_dir, "traces")
            xdg_dir = os.path.join(tmp_dir, "xdg")
            custom_cfg_path = os.path.join(
                xdg_dir, "adk-tokenomics", "models_config.json"
            )
            os.makedirs(traces_dir, exist_ok=True)
            os.makedirs(xdg_dir, exist_ok=True)

            res = _simulate_multi_agent_session_lifecycle(traces_dir)
            alpha_path = res["alpha_archive_path"]

            env_patch = {
                "XDG_CONFIG_HOME": xdg_dir,
                "TOKENOMICS_MODELS_CONFIG_PATH": custom_cfg_path,
                "TOKENOMICS_ALLOWED_HOSTS": "*.a.run.app",
            }
            with patch.dict(os.environ, env_patch, clear=False):
                TokenomicsWebHandler.set_server_context(
                    project_id="",
                    traces_path=traces_dir,
                    allowed_hosts=["*.a.run.app"],
                    bound_host="127.0.0.1",
                )
                httpd = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), TokenomicsWebHandler
                )
                port = httpd.server_address[1]
                base_url = f"http://127.0.0.1:{port}"
                srv_thread = threading.Thread(
                    target=httpd.serve_forever, daemon=True
                )
                srv_thread.start()
                try:
                    # 1. Security Matrix: DNS-rebinding GET & POST -> 403
                    st_rebind_get, _ = _http_request(
                        f"{base_url}/api/metrics",
                        headers={"Host": "evil.example"},
                    )
                    self.assertEqual(st_rebind_get, 403)

                    st_rebind_post, _ = _http_request(
                        f"{base_url}/api/models",
                        method="POST",
                        headers={
                            "Host": "evil.example",
                            "Origin": "http://evil.example",
                        },
                        json_body={
                            "id": "claude-opus-4",
                            "pricing": {"input": 1, "cached": 0.1, "output": 2},
                        },
                    )
                    self.assertEqual(st_rebind_post, 403)

                    # 2. Cross-origin POST -> 403
                    st_cors_post, _ = _http_request(
                        f"{base_url}/api/models",
                        method="POST",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": "https://attacker.example",
                        },
                        json_body={
                            "id": "claude-opus-4",
                            "pricing": {"input": 1, "cached": 0.1, "output": 2},
                        },
                    )
                    self.assertEqual(st_cors_post, 403)

                    # 3. Cloud Run HTTPS allowed-host flow (*.a.run.app) -> 200 on both GET and POST!
                    cloud_run_headers = {
                        "Host": "token-tower-prod-uc.a.run.app",
                        "Origin": "https://token-tower-prod-uc.a.run.app",
                    }
                    st_cr_get, cr_metrics_before = _http_request(
                        f"{base_url}/api/metrics?session_id=sess_e2e_alpha",
                        headers=cloud_run_headers,
                    )
                    self.assertEqual(st_cr_get, 200)
                    self.assertAlmostEqual(
                        cr_metrics_before["total_cost"], 0.152300, places=6
                    )

                    # Update claude-opus-4 rate card via POST /api/models (15/1.5/75 -> 20/2.0/100 => +$0.030000 on Turn 4)
                    st_post_model, post_resp = _http_request(
                        f"{base_url}/api/models",
                        method="POST",
                        headers=cloud_run_headers,
                        json_body={
                            "id": "claude-opus-4",
                            "name": "Claude Opus 4 Custom Enterprise",
                            "provider": "anthropic",
                            "pricing": {
                                "input": 20.0,
                                "cached": 2.0,
                                "output": 100.0,
                            },
                        },
                    )
                    self.assertEqual(st_post_model, 200, msg=str(post_resp))
                    self.assertTrue(os.path.exists(custom_cfg_path))

                    # Verify Web /api/metrics immediately reflects updated $0.182300 spend
                    st_after, metrics_after = _http_request(
                        f"{base_url}/api/metrics?session_id=sess_e2e_alpha"
                    )
                    self.assertEqual(st_after, 200)
                    self.assertAlmostEqual(
                        metrics_after["total_cost"], 0.182300, places=6
                    )

                    # Verify CLI profile & CLI assert subprocesses pick up the same user rate card override
                    cli_env = dict(os.environ)
                    cli_env["PYTHONPATH"] = REPO_ROOT
                    prof_after = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "adk_tokenomics_plugin.cli",
                            "profile",
                            "--traces",
                            alpha_path,
                            "--format",
                            "json",
                        ],
                        env=cli_env,
                        cwd=tmp_dir,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(
                        prof_after.returncode, 0, msg=prof_after.stderr
                    )
                    prof_after_cost = sum(
                        v["cost"]
                        for v in json.loads(prof_after.stdout)[
                            "sub_agent_breakdown"
                        ].values()
                    )
                    self.assertAlmostEqual(prof_after_cost, 0.182300, places=6)

                    assert_after = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "adk_tokenomics_plugin.cli",
                            "assert",
                            "--traces",
                            alpha_path,
                            "--format",
                            "json",
                        ],
                        env=cli_env,
                        cwd=tmp_dir,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(
                        assert_after.returncode, 0, msg=assert_after.stderr
                    )
                    assert_after_json = json.loads(assert_after.stdout)
                    self.assertAlmostEqual(
                        assert_after_json["avg_cost_per_turn"] * 5,
                        0.182300,
                        places=6,
                    )

                    # 4. Offline resilience: app.js boots & polls cleanly in Node VM WITHOUT Chart.js (Chart === undefined)
                    fe_offline = _run_node_frontend_vm(
                        base_url,
                        session_id="sess_e2e_alpha",
                        enable_chart=False,
                    )
                    self.assertEqual(fe_offline["runtimeErrors"], [])
                    self.assertEqual(fe_offline["chartCount"], 0)
                    self.assertEqual(
                        fe_offline["kpis"]["totalCostText"], "$0.18230"
                    )
                finally:
                    httpd.shutdown()
                    httpd.server_close()

        # Packaged adk_tokenomics_plugin/models_config.json must be 100% untouched!
        with open(pkg_models_path, "rb") as pf:
            sha256_after = hashlib.sha256(pf.read()).hexdigest()
        self.assertEqual(sha256_before, sha256_after)

    def test_scenario_4_bigquery_dual_source_e2e_csv_hash_and_packaging_smoke(
        self,
    ):
        bq_boundary = CapturingBigQueryBoundary(
            default_project="finops-e2e-proj"
        )
        google_pkg, cloud_pkg, bq_mod = bq_boundary.make_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            xdg_dir = os.path.join(tmp_dir, "xdg")
            os.makedirs(xdg_dir, exist_ok=True)
            env_vars = {
                "XDG_CONFIG_HOME": xdg_dir,
                "TOKENOMICS_MODELS_CONFIG_PATH": "",
                "TOKENOMICS_MODE": "prod",
                "TOKENOMICS_SINK": "bigquery",
            }

            with (
                patch.dict(
                    sys.modules,
                    {
                        "google": google_pkg,
                        "google.cloud": cloud_pkg,
                        "google.cloud.bigquery": bq_mod,
                    },
                ),
                patch.dict(os.environ, env_vars, clear=False),
            ):
                # Provision real ADK DDL schema on custom table `2026_adk-events` (with view_id="" first so base-table fallback is also verified)
                prov_ok = provision_bigquery_table(
                    project_id="finops-e2e-proj",
                    dataset_id="bq_adk_ds",
                    table_id="2026_adk-events",
                    view_id="",
                )
                self.assertTrue(prov_ok)
                table_fqn = "finops-e2e-proj.bq_adk_ds.2026_adk-events"
                self.assertIn(table_fqn, bq_boundary.table_schemas)
                schema_col_names = {
                    sf.name for sf in bq_boundary.table_schemas[table_fqn]
                }
                self.assertIn("attributes", schema_col_names)
                self.assertIn("content", schema_col_names)
                self.assertIn("event_type", schema_col_names)

                # Stream turns through BigQuerySink into hyphenated + leading-digit table `2026_adk-events`!
                bq_cfg = TokenomicsConfig(
                    mode="prod",
                    sink="bigquery",
                    project_id="finops-e2e-proj",
                    dataset_id="bq_adk_ds",
                    table_id="2026_adk-events",
                    view_id="2026_v-llm-response",
                    batch_size=2,
                    flush_interval_seconds=0.05,
                )
                sink = BigQuerySink(
                    bq_cfg, max_retries=1, retry_base_delay=0.01
                )
                tracker = TokenTracker(config=bq_cfg, sink=sink)

                # Include '#' and leading '=SUM' formula in prompt/response to also verify safe CSV export in Node VM!
                tracker.record_turn(
                    session_id="sess_bq_hash_1",
                    app_name="finops_router",
                    model_name="publishers/google/models/gemini-4-argon",
                    user_query="Analyze ticket #404 & C# cluster #spend-2026",
                    agent_response="=SUM(A1:A5) #flagged_formula_and_hash_fragment",
                    input_tokens=6000,
                    cached_tokens=4000,
                    output_tokens=1000,
                    thinking_tokens=500,
                    tools_called=["query_billing"],
                    skills_active=["bq-finops"],
                    latency_ms=120,
                    invocation_id="inv_bq_1",
                )
                tracker.record_turn(
                    session_id="sess_bq_hash_1",
                    app_name="finops_critic",
                    model_name="claude-opus-4",
                    user_query="Review #404 executive summary",
                    agent_response="Approved #ready-to-ship",
                    input_tokens=0,
                    cached_tokens=10000,
                    output_tokens=400,
                    thinking_tokens=600,
                    latency_ms=240,
                    invocation_id="inv_bq_2",
                )
                sink.close()

                self.assertIn(table_fqn, bq_boundary.tables)
                self.assertEqual(len(bq_boundary.tables[table_fqn]), 2)
                self.assertEqual(sink.dropped_rows_count, 0)
                first_content = json.loads(
                    bq_boundary.tables[table_fqn][0]["content"]
                )
                self.assertEqual(
                    first_content["usage"],
                    {"prompt": 10000, "completion": 1000, "total": 11500},
                )
                self.assertNotIn(
                    "tool_use", bq_boundary.tables[table_fqn][0]["attributes"]
                )

                # 1. BigQuerySink.fetch_recent -> FinOpsAggregator
                recent_rows = sink.fetch_recent(limit=10)
                self.assertEqual(len(recent_rows), 2)
                agg_res = FinOpsAggregator.aggregate_records(recent_rows)
                # Turn 1 ($0.0274) + Turn 2 ($0.0900) = $0.1174
                self.assertAlmostEqual(
                    agg_res["kpis"]["total_spend"], 0.11740, places=5
                )
                self.assertEqual(agg_res["kpis"]["total_turns"], 2)

                # 2. cli_profiler.profile_from_bigquery against `2026_adk-events` (base table fallback)
                prof_buf = io.StringIO()
                with redirect_stdout(prof_buf):
                    prof_recs = profile_from_bigquery(
                        session_id="sess_bq_hash_1",
                        project_id="finops-e2e-proj",
                        dataset_id="bq_adk_ds",
                        table_id="2026_adk-events",
                        output_format="json",
                    )
                self.assertIsNotNone(prof_recs)
                bq_prof_json = json.loads(prof_buf.getvalue())
                self.assertEqual(bq_prof_json["total_turns"], 2)
                self.assertEqual(bq_prof_json["source"], table_fqn)

                # 3. cli_finops.run_finops_summary_cli (view 404 -> automatic fallback to `2026_adk-events`)
                finops_buf = io.StringIO()
                with redirect_stdout(finops_buf):
                    finops_rc = run_finops_summary_cli(
                        project_id="finops-e2e-proj",
                        dataset_id="bq_adk_ds",
                        view_id="2026_v-llm-response",
                        table_id="2026_adk-events",
                        days=7,
                        limit_agents=5,
                        output_format="json",
                    )
                self.assertEqual(finops_rc, 0)
                finops_json = json.loads(finops_buf.getvalue())
                self.assertEqual(finops_json["source"], table_fqn)
                self.assertEqual(finops_json["total_turns"], 2)
                self.assertAlmostEqual(
                    finops_json["total_spend_usd"], 0.117400, places=6
                )
                self.assertAlmostEqual(
                    finops_json["optimization_savings_usd"], 0.142600, places=6
                )

                # 4. Live Web `/api/bq/stats`, `/api/bq/logs`, `/api/finops/summary` + Node VM `#` CSV export!
                TokenomicsWebHandler.set_server_context(
                    project_id="finops-e2e-proj",
                    dataset_id="bq_adk_ds",
                    table_id="2026_adk-events",
                    view_id="2026_v-llm-response",
                    traces_path=None,
                    bound_host="127.0.0.1",
                )
                httpd = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), TokenomicsWebHandler
                )
                port = httpd.server_address[1]
                base_url = f"http://127.0.0.1:{port}"
                srv_thread = threading.Thread(
                    target=httpd.serve_forever, daemon=True
                )
                srv_thread.start()
                try:
                    st_stats, bq_stats = _http_request(
                        f"{base_url}/api/bq/stats"
                    )
                    self.assertEqual(st_stats, 200)
                    self.assertEqual(bq_stats["status"], "success")
                    self.assertEqual(bq_stats["total_turns"], 2)
                    self.assertAlmostEqual(
                        bq_stats["total_cost"], 0.117400, places=6
                    )
                    self.assertEqual(bq_stats["total_fresh_input"], 6000)
                    self.assertEqual(bq_stats["total_cached"], 14000)

                    st_logs, bq_logs = _http_request(
                        f"{base_url}/api/bq/logs?limit=20&offset=0&search=%23404"
                    )
                    self.assertEqual(st_logs, 200)
                    self.assertEqual(bq_logs["status"], "success")
                    self.assertEqual(bq_logs["target"], table_fqn)
                    self.assertEqual(bq_logs["total_rows"], 2)

                    # Execute frontend app.js BigQuery tab + CSV export in Node VM and verify '#' & formula neutralization
                    fe_bq = _run_node_frontend_vm(
                        base_url, enable_chart=True, run_bq_csv=True
                    )
                    self.assertEqual(fe_bq["runtimeErrors"], [])
                    self.assertEqual(len(fe_bq["csvExports"]), 1)
                    csv_text = fe_bq["csvExports"][0]["csv"]
                    self.assertIn("#404 & C# cluster #spend-2026", csv_text)
                    self.assertIn("#ready-to-ship", csv_text)
                    self.assertIn(
                        "'=SUM(A1:A5) #flagged_formula_and_hash_fragment",
                        csv_text,
                    )
                finally:
                    httpd.shutdown()
                    httpd.server_close()

                # 4b. Provision `2026_v-llm-response` view (CREATE VIEW IF NOT EXISTS) and verify view-routed FinOps + DDL enforcement
                view_ok = provision_bigquery_table(
                    project_id="finops-e2e-proj",
                    dataset_id="bq_adk_ds",
                    table_id="2026_adk-events",
                    view_id="2026_v-llm-response",
                )
                self.assertTrue(view_ok)
                view_fqn = "finops-e2e-proj.bq_adk_ds.2026_v-llm-response"
                self.assertIn(view_fqn, bq_boundary.views)
                self.assertIn(
                    "CREATE VIEW IF NOT EXISTS", bq_boundary.view_ddls[view_fqn]
                )
                self.assertIn(
                    "usage_tool_use_tokens", bq_boundary.view_ddls[view_fqn]
                )

                finops_view_buf = io.StringIO()
                with redirect_stdout(finops_view_buf):
                    finops_view_rc = run_finops_summary_cli(
                        project_id="finops-e2e-proj",
                        dataset_id="bq_adk_ds",
                        view_id="2026_v-llm-response",
                        table_id="2026_adk-events",
                        days=7,
                        limit_agents=5,
                        output_format="json",
                    )
                self.assertEqual(finops_view_rc, 0)
                finops_view_json = json.loads(finops_view_buf.getvalue())
                self.assertEqual(finops_view_json["source"], view_fqn)
                self.assertAlmostEqual(
                    finops_view_json["total_spend_usd"], 0.117400, places=6
                )

                # Legacy flat table provisioned with legacy_flat_schema=True enforces flat DDL columns while BigQuerySink auto-detects flat
                flat_ok = provision_bigquery_table(
                    project_id="finops-e2e-proj",
                    dataset_id="bq_adk_ds",
                    table_id="legacy_v101_flat",
                    legacy_flat_schema=True,
                )
                self.assertTrue(flat_ok)
                flat_fqn = "finops-e2e-proj.bq_adk_ds.legacy_v101_flat"
                flat_sink = BigQuerySink(
                    TokenomicsConfig(
                        mode="prod",
                        sink="bigquery",
                        project_id="finops-e2e-proj",
                        dataset_id="bq_adk_ds",
                        table_id="legacy_v101_flat",
                        flush_interval_seconds=60.0,
                    ),
                    max_retries=0,
                )
                flat_sink.write_turn(
                    {
                        "session_id": "s_flat",
                        "input_tokens": 500,
                        "cached_tokens": 100,
                        "output_tokens": 50,
                    }
                )
                flat_sink.close()
                self.assertEqual(flat_sink.dropped_rows_count, 0)
                self.assertEqual(len(bq_boundary.tables[flat_fqn]), 1)
                self.assertIn("input_tokens", bq_boundary.tables[flat_fqn][0])
                self.assertNotIn("attributes", bq_boundary.tables[flat_fqn][0])

            # 5. Clean Packaging & Wheel Metadata Integrity Smoke Test
            pyproject_path = os.path.join(REPO_ROOT, "pyproject.toml")
            with open(pyproject_path, "rb") as tf:
                pyproject = tomllib.load(tf)

            proj_meta = pyproject["project"]
            self.assertIn(
                proj_meta["name"], ("adk-tokenomics", "adk-tokenomics-plugin")
            )
            self.assertEqual(proj_meta["version"], "1.0.2")
            self.assertEqual(
                proj_meta["dependencies"], ["python-dotenv>=1.0.0"]
            )
            scripts = proj_meta["scripts"]
            for cli_alias in ("adk-tokenomics", "adk-finops", "token-tower"):
                self.assertEqual(
                    scripts[cli_alias], "adk_tokenomics_plugin.cli:main"
                )

            pkg_data_patterns = pyproject["tool"]["setuptools"]["package-data"][
                "adk_tokenomics_plugin"
            ]
            self.assertIn("models_config.json", pkg_data_patterns)
            self.assertIn("web/*", pkg_data_patterns)

            # Build wheel archive (using setuptools.build_meta if present, else PEP 427 wheel.wheelfile.WheelFile)
            wheel_out_dir = os.path.join(tmp_dir, "dist")
            os.makedirs(wheel_out_dir, exist_ok=True)
            wheel_path: str | None = None
            try:
                from setuptools import build_meta  # type: ignore

                try:
                    built_name = build_meta.build_wheel(wheel_out_dir)
                    wheel_path = os.path.join(wheel_out_dir, built_name)
                finally:
                    shutil.rmtree(
                        os.path.join(REPO_ROOT, "build"), ignore_errors=True
                    )
                    for entry in os.listdir(REPO_ROOT):
                        if entry.endswith(".egg-info"):
                            shutil.rmtree(
                                os.path.join(REPO_ROOT, entry),
                                ignore_errors=True,
                            )
            except Exception:
                import glob as _glob

                from wheel.wheelfile import WheelFile

                wheel_path = os.path.join(
                    wheel_out_dir,
                    f"adk_tokenomics_plugin-{proj_meta['version']}-py3-none-any.whl",
                )
                pkg_src = os.path.join(REPO_ROOT, "adk_tokenomics_plugin")
                dist_info = (
                    f"adk_tokenomics_plugin-{proj_meta['version']}.dist-info"
                )
                with WheelFile(wheel_path, "w") as wf:
                    for root, _, files in os.walk(pkg_src):
                        if "__pycache__" in root:
                            continue
                        for fn in sorted(files):
                            if fn.endswith(".py"):
                                full = os.path.join(root, fn)
                                rel = os.path.relpath(full, REPO_ROOT)
                                wf.write(full, rel)
                    for pat in pkg_data_patterns:
                        for matched_file in sorted(
                            _glob.glob(os.path.join(pkg_src, pat))
                        ):
                            if os.path.isfile(matched_file):
                                wf.write(
                                    matched_file,
                                    os.path.relpath(matched_file, REPO_ROOT),
                                )
                    wf.writestr(
                        f"{dist_info}/METADATA",
                        f"Metadata-Version: 2.1\nName: {proj_meta['name']}\nVersion: {proj_meta['version']}\n",
                    )
                    wf.writestr(
                        f"{dist_info}/WHEEL",
                        "Wheel-Version: 1.0\nGenerator: e2e-smoke\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                    )

            self.assertTrue(wheel_path and os.path.exists(wheel_path))
            with zipfile.ZipFile(wheel_path, "r") as zf:
                names = set(zf.namelist())
                for required_asset in (
                    "adk_tokenomics_plugin/__init__.py",
                    "adk_tokenomics_plugin/models_config.json",
                    "adk_tokenomics_plugin/web/index.html",
                    "adk_tokenomics_plugin/web/app.js",
                    "adk_tokenomics_plugin/web/styles.css",
                ):
                    self.assertIn(required_asset, names)

                isolated_site = os.path.join(tmp_dir, "isolated_site")
                empty_cwd = os.path.join(tmp_dir, "empty_cwd")
                os.makedirs(isolated_site, exist_ok=True)
                os.makedirs(empty_cwd, exist_ok=True)
                zf.extractall(isolated_site)

            smoke_code = textwrap.dedent(
                f"""\
                import os, sys
                sys.path = [{isolated_site!r}] + [p for p in sys.path if {REPO_ROOT!r} not in p]
                import adk_tokenomics_plugin as pkg
                assert pkg.__file__.startswith({isolated_site!r}), pkg.__file__
                tower = pkg.TokenControlTower(mode="dev")
                rec = tower.track_turn(
                    session_id="s_wheel",
                    app_name="wheel_smoke",
                    model_name="gpt-5-mini",
                    user_query="Ping",
                    agent_response="Pong",
                    input_tokens=1000,
                    cached_tokens=0,
                    output_tokens=100,
                )
                assert abs(rec["total_cost"] - 0.00021) < 1e-9, rec["total_cost"]
                assert os.path.exists(pkg.resolve_models_config_path())
                """
            )
            smoke_proc = subprocess.run(
                [sys.executable, "-c", smoke_code],
                cwd=empty_cwd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(smoke_proc.returncode, 0, msg=smoke_proc.stderr)


if __name__ == "__main__":
    unittest.main()
