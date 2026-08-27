from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import ctypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

import websocket


APP_NAME = "领星数字SKU清理助手"
APP_VERSION = "1.1.4"
LIST_URL = "https://oms.xlwms.com/platform/order/list"
LOGIN_URL_PART = "/login"
DEBUG_PORT = 19225
MAX_CANDIDATES = 200
_INSTANCE_MUTEX: int | None = None


def app_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return root / "LingxingNumericSkuCleaner"


DATA_DIR = app_data_dir()
PROFILE_DIR = DATA_DIR / "ChromeProfile"
REPORT_DIR = DATA_DIR / "reports"


def normalize_blank(value: Any) -> bool:
    return str(value or "").strip() in {"", "-"}


def is_numeric_platform_sku(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.isascii() and text.isdigit()


def should_remove_product_row(row: dict[str, Any]) -> bool:
    return (
        is_numeric_platform_sku(row.get("platformSku"))
        and normalize_blank(row.get("sku"))
        and normalize_blank(row.get("productName"))
        and normalize_blank(row.get("availableInventory"))
    )


def choose_removal_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return removable rows from bottom to top, but never remove every row."""
    matches = [row for row in rows if should_remove_product_row(row)]
    allowed = max(0, len(rows) - 1)
    if len(matches) > allowed:
        matches = matches[-allowed:] if allowed else []
    return list(reversed(matches))


def assemble_list_state(
    start: dict[str, Any],
    end: dict[str, Any],
    chunks: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Merge overlapping virtual-table viewports into one verified list snapshot."""
    seen_order: list[str] = []
    seen_ids: set[str] = set()
    hydrated: dict[str, dict[str, str]] = {}
    max_rendered = 0
    for chunk in chunks:
        max_rendered = max(max_rendered, len(chunk))
        for row in chunk:
            system_id = str(row.get("systemOrderId", "")).strip()
            if not system_id:
                continue
            if system_id not in seen_ids:
                seen_ids.add(system_id)
                seen_order.append(system_id)
            platform_id = str(row.get("platformOrderId", "")).strip()
            sku_text = str(row.get("platformSkuText", "")).strip()
            if platform_id and sku_text:
                hydrated[system_id] = {
                    "systemOrderId": system_id,
                    "platformOrderId": platform_id,
                    "platformSkuText": sku_text,
                }

    count_keys = ("total", "tabTotal", "pageSize")
    counts_stable = all(start.get(key) == end.get(key) for key in count_keys)
    total = int(end.get("total") or 0)
    page_size = int(end.get("pageSize") or 0)
    expected = min(total, page_size) if page_size else 0
    candidates = [
        hydrated[system_id]
        for system_id in seen_order
        if system_id in hydrated and "多个" in hydrated[system_id]["platformSkuText"]
    ]
    return {
        "ready": (
            counts_stable
            and end.get("tabTotal") == total
            and len(seen_ids) == expected
            and len(hydrated) == expected
        ),
        "scanned": len(seen_ids),
        "total": total,
        "tabTotal": end.get("tabTotal"),
        "pageSize": page_size,
        "allOrderIds": seen_order,
        "candidates": candidates,
        "virtualized": expected > max_rendered,
        "maxRenderedRows": max_rendered,
    }


def acquire_single_instance() -> bool:
    """Prevent two automation windows from operating the same Chrome session."""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, "Local\\LingxingNumericSkuCleaner_v1")
    if not handle:
        return False
    _INSTANCE_MUTEX = int(handle)
    return kernel32.GetLastError() != 183


@dataclass
class OrderResult:
    system_order_id: str
    platform_order_id: str
    status: str
    removed_skus: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class RunReport:
    started_at: str
    mode: str
    finished_at: str = ""
    scanned_orders: int = 0
    candidate_orders: int = 0
    modified_orders: int = 0
    removed_rows: int = 0
    skipped_orders: int = 0
    error_orders: int = 0
    remaining_multiple: int = 0
    stopped_early: bool = False
    stop_reason: str = ""
    results: list[OrderResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


class CDPError(RuntimeError):
    pass


class CDPPage:
    def __init__(self, websocket_url: str):
        self.ws = websocket.create_connection(
            websocket_url,
            timeout=20,
            origin=f"http://127.0.0.1:{DEBUG_PORT}",
            suppress_origin=False,
        )
        self.counter = 0
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def command(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
        self.counter += 1
        message_id = self.counter
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            response = json.loads(raw)
            if response.get("id") != message_id:
                if response.get("method") == "Page.javascriptDialogOpening":
                    self.events.append(response)
                continue
            if "error" in response:
                raise CDPError(f"{method}: {response['error'].get('message', response['error'])}")
            return response.get("result", {})

    def take_events(self, method: str) -> list[dict[str, Any]]:
        matched = [event for event in self.events if event.get("method") == method]
        self.events = [event for event in self.events if event.get("method") != method]
        return matched

    def evaluate(self, expression: str, timeout: float = 30) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout,
        )
        payload = result.get("result", {})
        if payload.get("subtype") == "error":
            raise CDPError(payload.get("description", "页面脚本执行失败"))
        return payload.get("value")

    def navigate(self, url: str) -> None:
        self.command("Page.navigate", {"url": url})

    def url(self) -> str:
        return str(self.evaluate("location.href") or "")

    def body_text(self) -> str:
        return str(self.evaluate("document.body ? document.body.innerText : ''") or "")


class LingxingAutomation:
    def __init__(self, log: Callable[[str], None], progress: Callable[[int, int], None]):
        self.log = log
        self.progress = progress
        self.chrome_process: subprocess.Popen[Any] | None = None
        self.page: CDPPage | None = None
        self.stop_requested = False

    def request_stop(self) -> None:
        self.stop_requested = True

    def _find_chrome(self) -> Path:
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for path in candidates:
            if path.is_file():
                return path
        found = shutil.which("chrome") or shutil.which("chrome.exe")
        if found:
            return Path(found)
        raise RuntimeError("未找到 Google Chrome，请先安装 Chrome。")

    @staticmethod
    def _debug_endpoint(path: str = "/json") -> str:
        return f"http://127.0.0.1:{DEBUG_PORT}{path}"

    def _debug_ready(self) -> bool:
        try:
            with urllib.request.urlopen(self._debug_endpoint("/json/version"), timeout=1) as response:
                return response.status == 200
        except Exception:
            return False

    def start_browser(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if not self._debug_ready():
            chrome = self._find_chrome()
            args = [
                str(chrome),
                f"--remote-debugging-port={DEBUG_PORT}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-allow-origins=http://127.0.0.1:{DEBUG_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                LIST_URL,
            ]
            self.chrome_process = subprocess.Popen(args)
            self.log("已打开专用 Chrome，会话只保存在本机应用数据目录。")
        deadline = time.time() + 20
        while time.time() < deadline and not self._debug_ready():
            time.sleep(0.3)
        if not self._debug_ready():
            raise RuntimeError("无法连接专用 Chrome，请关闭该程序打开的 Chrome 后重试。")
        self._connect_page()

    def _targets(self) -> list[dict[str, Any]]:
        with urllib.request.urlopen(self._debug_endpoint("/json"), timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def _connect_page(self) -> None:
        deadline = time.time() + 15
        selected: dict[str, Any] | None = None
        while time.time() < deadline:
            targets = [item for item in self._targets() if item.get("type") == "page"]
            selected = next((item for item in targets if "oms.xlwms.com" in item.get("url", "")), None)
            if selected:
                break
            time.sleep(0.4)
        if not selected:
            raise RuntimeError("未找到领星页面。")
        if self.page:
            self.page.close()
        self.page = CDPPage(selected["webSocketDebuggerUrl"])
        self.page.command("Page.enable")
        self.page.command("Runtime.enable")

    def _wait(self, predicate: Callable[[], Any], timeout: float, description: str) -> Any:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.stop_requested:
                raise RuntimeError("用户已停止运行。")
            try:
                result = predicate()
                if result:
                    return result
            except Exception as exc:
                last_error = exc
            time.sleep(0.35)
        suffix = f"（{last_error}）" if last_error else ""
        raise RuntimeError(f"等待{description}超时{suffix}")

    def ensure_logged_in(self) -> None:
        assert self.page
        current = self.page.url()
        if LOGIN_URL_PART in current:
            self.log("首次使用：请在 Chrome 窗口完成领星登录，程序会自动继续（最多等待10分钟）。")
        self._wait(lambda: LOGIN_URL_PART not in self.page.url(), 600, "领星登录")

    def open_pending_list(self, refresh: bool = False) -> None:
        assert self.page
        if refresh or "/platform/order/list" not in self.page.url():
            self.page.navigate(LIST_URL)
        self._wait(lambda: "平台SKU*数量" in self.page.body_text(), 30, "订单列表加载")
        clicked = self.page.evaluate(
            """(() => {
              const tabs=[...document.querySelectorAll('[role=tab],.el-tabs__item')];
              const tab=tabs.find(e => /^待处理(?:\\s|\\()/.test((e.innerText||'').trim()));
              if(!tab) return false;
              if(tab.getAttribute('aria-selected')!=='true' && !tab.classList.contains('is-active')) tab.click();
              return true;
            })()"""
        )
        if not clicked:
            raise RuntimeError("未找到“待处理”页签，页面结构可能已更新。")
        self._wait(
            lambda: bool(
                self.page.evaluate(
                    """(() => [...document.querySelectorAll('[role=tab],.el-tabs__item')]
                      .some(e => /^待处理(?:\\s|\\()/.test((e.innerText||'').trim()) &&
                        (e.getAttribute('aria-selected')==='true' || e.classList.contains('is-active'))))()"""
                )
            ),
            15,
            "待处理页签切换",
        )
        time.sleep(0.5)
        self._wait(
            lambda: bool(
                self.page.evaluate(
                    """(() => !!document.querySelector('.el-pagination__total'))()"""
                )
            ),
            25,
            "待处理订单数据",
        )
        self._set_page_size_for_all()
        self._wait(
            lambda: bool(
                self.page.evaluate(
                    """(() => {
                      const total=Number(((document.querySelector('.el-pagination__total')?.innerText||'')
                        .match(/\\d+/)||['0'])[0]);
                      const rows=document.querySelectorAll(
                        '.vxe-table--body-wrapper.body--wrapper tr[rowid]').length;
                      return total===0 || rows>0;
                    })()"""
                )
            ),
            25,
            "订单表格首屏",
        )

    def _pagination_state(self) -> dict[str, int]:
        assert self.page
        result = self.page.evaluate(
            """(() => {
              const digits=(text)=>Number(((text||'').match(/\\d+/)||['0'])[0]);
              const total=digits(document.querySelector('.el-pagination__total')?.innerText);
              const pageSize=digits(document.querySelector('.el-pagination__sizes input')?.value);
              return {total,pageSize};
            })()"""
        )
        if not isinstance(result, dict):
            return {"total": 0, "pageSize": 0}
        return {"total": int(result.get("total", 0)), "pageSize": int(result.get("pageSize", 0))}

    def _set_page_size_for_all(self) -> None:
        """Fit all pending orders on one page so a single candidate queue is complete."""
        assert self.page
        pagination = self._pagination_state()
        total = pagination["total"]
        current_size = pagination["pageSize"]
        available_sizes = [100, 200, 500, 1000, 2000]
        target_size = next((size for size in available_sizes if size >= total), None)
        if target_size is None:
            raise RuntimeError("待处理订单超过 2000 笔，超出单页安全扫描上限，已停止。")
        if current_size >= total and current_size >= 100:
            return
        opened = self.page.evaluate(
            """(() => {
              const input=document.querySelector('.el-pagination__sizes input');
              if(!input) return false;
              input.click();
              return true;
            })()"""
        )
        if not opened:
            raise RuntimeError("未找到分页数量选择器，无法保证完整扫描。")
        time.sleep(0.5)
        selected = self.page.evaluate(
            f"""(() => {{
              const target={target_size};
              const options=[...document.querySelectorAll('.el-select-dropdown__item')]
                .filter(e => e.offsetParent!==null);
              const option=options.find(e => Number((((e.innerText||'').match(/\\d+/)||['0'])[0]))===target);
              if(!option) return false;
              option.click(); return true;
            }})()"""
        )
        if not selected:
            raise RuntimeError(f"分页器中没有 {target_size} 条/页选项，无法保证完整扫描。")
        self._wait(lambda: self._pagination_state()["pageSize"] == target_size, 10, "分页数量更新")
        time.sleep(0.5)

    @staticmethod
    def _virtual_scroll_positions(scroll_height: int, client_height: int) -> list[int]:
        max_scroll = max(0, scroll_height - client_height)
        if max_scroll == 0:
            return [0]
        step = max(120, int(client_height * 0.7))
        positions = list(range(0, max_scroll, step))
        if not positions or positions[-1] != max_scroll:
            positions.append(max_scroll)
        return positions

    def _read_list_state(self) -> dict[str, Any]:
        """Read every row of a VXE table, including rows outside the rendered viewport."""
        assert self.page
        metadata_script = """(() => {
          const headers=[...document.querySelectorAll('table.vxe-table--header th')]
            .map(th=>({text:(th.innerText||'').replace(/\\s+/g,'').trim(), colid:th.getAttribute('colid')}));
          const col=(name)=>headers.find(h=>h.text===name)?.colid;
          const platformCol=col('平台单号');
          const skuCol=col('平台SKU*数量');
          const digits=(text)=>Number(((text||'').match(/\\d+/)||['0'])[0]);
          const total=digits(document.querySelector('.el-pagination__total')?.innerText);
          const pageSize=digits(document.querySelector('.el-pagination__sizes input')?.value);
          const pendingTab=[...document.querySelectorAll('[role=tab],.el-tabs__item')]
            .find(e=>/^待处理(?:\\s|\\()/.test((e.innerText||'').trim()));
          const tabMatch=(pendingTab?.innerText||'').match(/\\((\\d+)\\)/);
          const tabTotal=tabMatch ? Number(tabMatch[1]) : null;
          const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
          return {
            valid:!!platformCol&&!!skuCol&&!!pageSize&&tabTotal!==null&&!!wrapper,
            platformCol,skuCol,total,tabTotal,pageSize,
            clientHeight:wrapper?.clientHeight||0,
            scrollHeight:wrapper?.scrollHeight||0,
            originalScrollTop:wrapper?.scrollTop||0
          };
        })()"""
        start = self.page.evaluate(metadata_script)
        if not isinstance(start, dict) or not start.get("valid"):
            return {"ready": False, "scanned": 0, "candidates": []}

        positions = self._virtual_scroll_positions(
            int(start.get("scrollHeight") or 0),
            int(start.get("clientHeight") or 0),
        )
        platform_col = json.dumps(str(start["platformCol"]))
        sku_col = json.dumps(str(start["skuCol"]))
        original_scroll = int(start.get("originalScrollTop") or 0)
        chunks: list[list[dict[str, Any]]] = []
        try:
            for position in positions:
                if self.stop_requested:
                    raise RuntimeError("用户已停止运行。")
                self.page.evaluate(
                    f"""(() => {{
                      const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
                      if(!wrapper) return false;
                      wrapper.scrollTop={position};
                      wrapper.dispatchEvent(new Event('scroll',{{bubbles:true}}));
                      return true;
                    }})()"""
                )
                time.sleep(0.1)
                chunk = self.page.evaluate(
                    f"""(() => {{
                      const platformCol={platform_col}, skuCol={sku_col};
                      const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
                      if(!wrapper) return [];
                      return [...wrapper.querySelectorAll('tr[rowid]')].map(row=>{{
                        const systemOrderId=row.getAttribute('rowid')||'';
                        const platformSkuText=(row.querySelector(
                          'td[colid="'+skuCol+'"]')?.innerText||'').trim();
                        const platformOrderText=(row.querySelector(
                          'td[colid="'+platformCol+'"]')?.innerText||'').trim();
                        const platformOrderId=platformOrderText.split(/\\s+/)[0]||'';
                        return {{systemOrderId,platformOrderId,platformSkuText}};
                      }});
                    }})()"""
                )
                chunks.append(chunk if isinstance(chunk, list) else [])
        finally:
            try:
                self.page.evaluate(
                    f"""(() => {{
                      const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
                      if(!wrapper) return false;
                      wrapper.scrollTop={original_scroll};
                      wrapper.dispatchEvent(new Event('scroll',{{bubbles:true}}));
                      return true;
                    }})()"""
                )
            except Exception:
                pass

        end = self.page.evaluate(metadata_script)
        if not isinstance(end, dict):
            return {"ready": False, "scanned": 0, "candidates": []}
        return assemble_list_state(start, end, chunks)

    @staticmethod
    def _candidate_signature(state: dict[str, Any]) -> str:
        """Compare candidates without depending on volatile list order or counts."""
        candidates = [
            {
                "systemOrderId": str(item.get("systemOrderId", "")),
                "platformOrderId": str(item.get("platformOrderId", "")),
                "platformSkuText": str(item.get("platformSkuText", "")),
            }
            for item in state.get("candidates", [])
            if isinstance(item, dict)
        ]
        candidates.sort(key=lambda item: (item["systemOrderId"], item["platformOrderId"]))
        return json.dumps(candidates, ensure_ascii=False, sort_keys=True)

    def _read_stable_list_state(self, timeout: float = 30) -> dict[str, Any]:
        """Return consecutive complete candidate snapshots despite unrelated order churn."""
        deadline = time.time() + timeout
        previous_signature: str | None = None
        consistent_reads = 0
        last_state: dict[str, Any] = {"ready": False, "scanned": 0, "candidates": []}
        while time.time() < deadline:
            if self.stop_requested:
                raise RuntimeError("用户已停止运行。")
            last_state = self._read_list_state()
            if last_state.get("ready"):
                signature = self._candidate_signature(last_state)
                if signature == previous_signature:
                    consistent_reads += 1
                else:
                    consistent_reads = 1
                previous_signature = signature
                # Empty results receive one extra complete read to prevent a
                # transient loading state from being reported as zero candidates.
                required_reads = 3 if not last_state.get("candidates") else 2
                if consistent_reads >= required_reads:
                    return last_state
            else:
                previous_signature = None
                consistent_reads = 0
            time.sleep(0.5)
        details = {
            "total": last_state.get("total"),
            "tabTotal": last_state.get("tabTotal"),
            "scanned": last_state.get("scanned"),
            "pageSize": last_state.get("pageSize"),
            "maxRenderedRows": last_state.get("maxRenderedRows"),
            "virtualized": last_state.get("virtualized"),
        }
        raise RuntimeError(
            f"订单列表在 {timeout:.0f} 秒内未形成完整快照："
            f"{json.dumps(details, ensure_ascii=False, sort_keys=True)}"
        )

    def _click_edit(self, system_order_id: str) -> None:
        """Find an order across virtual rows, then click only its Edit button."""
        assert self.page
        metrics = self.page.evaluate(
            """(() => {
              const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
              return wrapper ? {
                clientHeight:wrapper.clientHeight,scrollHeight:wrapper.scrollHeight,
                originalScrollTop:wrapper.scrollTop
              } : null;
            })()"""
        )
        if not isinstance(metrics, dict):
            raise RuntimeError("未找到订单表格，无法定位编辑按钮。")
        positions = self._virtual_scroll_positions(
            int(metrics.get("scrollHeight") or 0),
            int(metrics.get("clientHeight") or 0),
        )
        original_scroll = int(metrics.get("originalScrollTop") or 0)
        safe_id = json.dumps(system_order_id)
        clicked = False
        try:
            for position in positions:
                if self.stop_requested:
                    raise RuntimeError("用户已停止运行。")
                self.page.evaluate(
                    f"""(() => {{
                      const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
                      if(!wrapper) return false;
                      wrapper.scrollTop={position};
                      wrapper.dispatchEvent(new Event('scroll',{{bubbles:true}}));
                      return true;
                    }})()"""
                )
                time.sleep(0.1)
                clicked = bool(
                    self.page.evaluate(
                        f"""(() => {{
                          const safe={safe_id};
                          const rows=[...document.querySelectorAll('table.vxe-table--body tr[rowid]')]
                            .filter(row=>row.getAttribute('rowid')===safe);
                          const button=rows.flatMap(row=>[...row.querySelectorAll('button')])
                            .find(item=>(item.innerText||'').trim()==='编辑');
                          if(!button) return false;
                          button.click();
                          return true;
                        }})()"""
                    )
                )
                if clicked:
                    break
        finally:
            if not clicked:
                try:
                    self.page.evaluate(
                        f"""(() => {{
                          const wrapper=document.querySelector('.vxe-table--body-wrapper.body--wrapper');
                          if(!wrapper) return false;
                          wrapper.scrollTop={original_scroll};
                          wrapper.dispatchEvent(new Event('scroll',{{bubbles:true}}));
                          return true;
                        }})()"""
                    )
                except Exception:
                    pass
        if not clicked:
            raise RuntimeError("候选订单已不在当前待处理列表，未执行任何修改。")
        self._wait(lambda: f"/platform/order/edit/{system_order_id}" in self.page.url(), 20, "订单编辑页")
        self._wait(lambda: "产品信息" in self.page.body_text(), 20, "产品信息")
        self._wait(
            lambda: bool(
                self.page.evaluate(
                    """(() => {
                      const rows=[...document.querySelectorAll('table.vxe-table--body tbody tr[rowid]')];
                      return rows.length>0;
                    })()"""
                )
            ),
            20,
            "订单产品行加载",
        )

    def _read_edit_state(self) -> dict[str, Any]:
        assert self.page
        script = """(() => {
          const headers=[...document.querySelectorAll('table.vxe-table--header th')]
            .map(th=>({text:(th.innerText||'').replace(/\\s+/g,'').trim(),colid:th.getAttribute('colid')}));
          const col=(name)=>headers.find(h=>h.text===name)?.colid;
          const ids={platformSku:col('平台SKU'),sku:col('SKU'),productName:col('产品名称'),
                     availableInventory:col('可用库存'),quantity:col('数量'),operation:col('操作')};
          if(Object.values(ids).some(v=>!v)) return {ready:false,rows:[],outsideInputs:[]};
          const body=[...document.querySelectorAll('table.vxe-table--body')]
            .find(t=>t.querySelector(`td[colid="${ids.platformSku}"]`));
          if(!body) return {ready:false,rows:[],outsideInputs:[]};
          const val=(td)=>{
            if(!td) return '';
            const input=td.querySelector('input,textarea');
            return (input ? input.value : td.innerText || '').trim();
          };
          const rows=[...body.querySelectorAll('tr[rowid]')].map(tr=>({
            rowId:tr.getAttribute('rowid'),
            platformSku:val(tr.querySelector(`td[colid="${ids.platformSku}"]`)),
            sku:val(tr.querySelector(`td[colid="${ids.sku}"]`)),
            productName:val(tr.querySelector(`td[colid="${ids.productName}"]`)),
            availableInventory:val(tr.querySelector(`td[colid="${ids.availableInventory}"]`)),
            quantity:val(tr.querySelector(`td[colid="${ids.quantity}"]`))
          }));
          const controls=[...document.querySelectorAll('input,textarea,select')]
            .filter(e=>!e.closest('table.vxe-table--body'))
            .map((e,i)=>({i,tag:e.tagName,type:e.type||'',name:e.name||'',placeholder:e.placeholder||'',value:e.value||''}));
          return {ready:true,rows,outsideInputs:controls,ids};
        })()"""
        state = self.page.evaluate(script)
        if not isinstance(state, dict) or not state.get("ready"):
            raise RuntimeError("无法识别产品信息表，页面结构可能已更新。")
        return state

    def _wait_edit_form_stable(self, timeout: float = 20, stable_seconds: float = 1.5) -> None:
        """Wait for Lingxing to hydrate untouched order fields before any mutation."""
        assert self.page
        deadline = time.time() + timeout
        previous = ""
        stable_since: float | None = None
        while time.time() < deadline:
            state = self._read_edit_state()
            country_ready = bool(
                self.page.evaluate(
                    """(() => {
                      const items=[...document.querySelectorAll('.el-form-item')];
                      const item=items.find(e=>(e.querySelector('.el-form-item__label')?.innerText||'')
                        .includes('国家/地区'));
                      const input=item?.querySelector('input');
                      return !!item && !!(input?.value||'').trim() &&
                        item.classList.contains('is-success') && !item.classList.contains('is-error');
                    })()"""
                )
            )
            signature = json.dumps(state["outsideInputs"], ensure_ascii=False, sort_keys=True)
            if country_ready and signature == previous:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= stable_seconds:
                    return
            else:
                stable_since = None
            previous = signature
            time.sleep(0.3)
        raise RuntimeError("订单原始表单在20秒内未完成加载，已停止且未修改。")

    def _remove_row(self, row_id: str) -> None:
        assert self.page
        before_count = len(self._read_edit_state()["rows"])
        self.page.take_events("Page.javascriptDialogOpening")
        clicked = self.page.evaluate(
            f"""(() => {{
              const id={json.dumps(row_id)};
              const row=[...document.querySelectorAll('table.vxe-table--body tr[rowid]')]
                .find(r=>r.getAttribute('rowid')===id && [...r.querySelectorAll('*')]
                .some(e=>e.children.length===0 && (e.innerText||'').trim()==='移除'));
              if(!row) return false;
              const remove=[...row.querySelectorAll('*')]
                .find(e=>e.children.length===0 && (e.innerText||'').trim()==='移除');
              if(!remove) return false;
              remove.click(); return true;
            }})()"""
        )
        if not clicked:
            raise RuntimeError("无法精确定位目标行的“移除”按钮。")

        # Lingxing uses a fixed-position Element UI dialog. Fixed wrappers can have
        # offsetParent === null even when visible, so visibility must use computed
        # style and the rendered rectangle instead.
        deadline = time.time() + 8
        transition = ""
        while time.time() < deadline:
            native_dialogs = self.page.take_events("Page.javascriptDialogOpening")
            if native_dialogs:
                params = native_dialogs[-1].get("params", {})
                if params.get("type") == "confirm" and "移除" in str(params.get("message", "")):
                    self.page.command("Page.handleJavaScriptDialog", {"accept": True})
                    transition = "confirmed"
                    break
                raise RuntimeError("出现了非平台SKU移除用途的浏览器确认框，已停止。")
            transition = str(
                self.page.evaluate(
                    f"""(() => {{
                      const before={before_count};
                      const rowCount=[...document.querySelectorAll('table.vxe-table--body tr[rowid]')].length;
                      if(rowCount===before-1) return 'removed';
                      const visible=(e)=>{{
                        const style=getComputedStyle(e);
                        const rect=e.getBoundingClientRect();
                        return style.display!=='none' && style.visibility!=='hidden' &&
                          Number(style.opacity||1)>0 && rect.width>0 && rect.height>0;
                      }};
                      const dialogs=[...document.querySelectorAll('#ak-confirm.comfirm-dialog,.el-dialog__wrapper')]
                        .filter(visible);
                      if(!dialogs.length) return '';
                      const dialog=dialogs.find(e=>(e.innerText||'').includes('移除平台SKU'));
                      if(!dialog) return 'error:出现了非“移除平台SKU”的弹窗';
                      const text=(dialog.innerText||'').replace(/\\s+/g,'');
                      if(!text.includes('是否确认移除该平台SKU'))
                        return 'error:移除弹窗提示内容不符合预期';
                      const buttons=[...dialog.querySelectorAll('button')].filter(visible);
                      const confirm=buttons.find(b=>(b.innerText||'').trim()==='移除');
                      if(!confirm) return 'error:移除弹窗中没有找到“移除”确认按钮';
                      confirm.click();
                      return 'confirmed';
                    }})()"""
                )
                or ""
            )
            if transition in {"removed", "confirmed"}:
                break
            if transition.startswith("error:"):
                raise RuntimeError(transition.removeprefix("error:"))
            time.sleep(0.25)
        if transition not in {"removed", "confirmed"}:
            raise RuntimeError("点击产品行“移除”后，8秒内未出现平台SKU确认框。")
        self._wait(lambda: len(self._read_edit_state()["rows"]) == before_count - 1, 10, "目标行移除")

    @staticmethod
    def _product_signature(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str]]:
        return [
            (
                str(row.get("platformSku", "")),
                str(row.get("sku", "")),
                str(row.get("productName", "")),
                str(row.get("availableInventory", "")),
                str(row.get("quantity", "")),
            )
            for row in rows
        ]

    def _click_save(self) -> None:
        assert self.page
        clicked = self.page.evaluate(
            """(() => {
              const visible=(e)=>{
                const style=getComputedStyle(e), rect=e.getBoundingClientRect();
                return style.display!=='none' && style.visibility!=='hidden' && rect.width>0 && rect.height>0;
              };
              const buttons=[...document.querySelectorAll('button')]
                .filter(b=>visible(b) && (b.innerText||'').trim()==='保存');
              if(buttons.length!==1) return false;
              buttons[0].click(); return true;
            })()"""
        )
        if not clicked:
            raise RuntimeError("“保存”按钮不是唯一精确匹配，已停止以避免误操作。")

        # Saving does not consistently navigate back to the list. Wait for the
        # request/UI to settle, capture explicit failures, then verify from a
        # freshly loaded list instead of clicking Save again.
        started = time.time()
        deadline = started + 10
        saw_loading = False
        while time.time() < deadline:
            if "/platform/order/list" in self.page.url():
                return
            status = self.page.evaluate(
                """(() => {
                  const visible=(e)=>{
                    const style=getComputedStyle(e), rect=e.getBoundingClientRect();
                    return style.display!=='none' && style.visibility!=='hidden' &&
                      Number(style.opacity||1)>0 && rect.width>0 && rect.height>0;
                  };
                  const save=[...document.querySelectorAll('button')]
                    .find(b=>visible(b) && (b.innerText||'').trim()==='保存');
                  const messages=[...document.querySelectorAll('.el-message,.el-notification')]
                    .filter(visible).map(e=>(e.innerText||'').trim()).filter(Boolean);
                  const country=[...document.querySelectorAll('.el-form-item')]
                    .find(e=>(e.querySelector('.el-form-item__label')?.innerText||'').includes('国家/地区'));
                  const countryEmpty=!!country && country.classList.contains('is-error') &&
                    !(country.querySelector('input')?.value||'').trim();
                  return {loading:!!save && (save.classList.contains('is-loading')||save.disabled),
                    messages,countryEmpty};
                })()"""
            ) or {}
            messages = [str(item) for item in status.get("messages", [])]
            failure = next(
                (text for text in messages if any(word in text for word in ["异常", "失败", "不能为空", "错误"])),
                "",
            )
            if status.get("countryEmpty"):
                raise RuntimeError("保存被领星校验拦截：国家/地区尚未完成加载。")
            if failure:
                raise RuntimeError(f"领星保存失败：{failure}")
            loading = bool(status.get("loading"))
            saw_loading = saw_loading or loading
            if saw_loading and not loading:
                break
            if not loading and time.time() - started >= 5:
                break
            time.sleep(0.25)
        if "/platform/order/list" not in self.page.url():
            self.page.navigate(LIST_URL)
            self._wait(lambda: "/platform/order/list" in self.page.url(), 15, "保存后打开订单列表")

    def _verify_saved_from_list(self, system_order_id: str, expected_remaining: list[dict[str, Any]]) -> None:
        self.open_pending_list(refresh=True)
        state = self._read_stable_list_state()
        all_order_ids = set(state.get("allOrderIds", []))
        if system_order_id not in all_order_ids:
            raise RuntimeError("保存后复核失败：订单不在待处理列表，已停止且不会重试保存。")
        candidate_ids = {item.get("systemOrderId") for item in state.get("candidates", [])}
        if len(expected_remaining) == 1:
            if system_order_id in candidate_ids:
                raise RuntimeError("保存后复核失败：订单仍显示“多个”，目标SKU可能未保存。")
            return

        self._click_edit(system_order_id)
        verified = self._read_edit_state()
        if self._product_signature(verified["rows"]) != self._product_signature(expected_remaining):
            self.open_pending_list(refresh=True)
            raise RuntimeError("保存后详情复核不一致，已停止且不会重试保存。")
        self.open_pending_list(refresh=True)

    def process_order(self, candidate: dict[str, Any], scan_only: bool) -> OrderResult:
        system_id = candidate["systemOrderId"]
        platform_id = candidate.get("platformOrderId", "")
        self._click_edit(system_id)
        if not scan_only:
            self._wait_edit_form_stable()
        initial = self._read_edit_state()
        rows = initial["rows"]
        targets = choose_removal_targets(rows)
        if not targets:
            self.open_pending_list()
            return OrderResult(system_id, platform_id, "skipped", detail="没有同时满足五项条件的可删除行")
        if scan_only:
            skus = [row["platformSku"] for row in targets]
            self.open_pending_list()
            return OrderResult(system_id, platform_id, "scan", removed_skus=skus, detail="只读预检，未修改")

        target_ids = {row["rowId"] for row in targets}
        expected_remaining = [row for row in rows if row["rowId"] not in target_ids]
        for target in targets:
            self._remove_row(target["rowId"])

        after = self._read_edit_state()
        if after["outsideInputs"] != initial["outsideInputs"]:
            raise RuntimeError("检测到产品表以外字段发生变化，已停止保存。")
        if self._product_signature(after["rows"]) != self._product_signature(expected_remaining):
            raise RuntimeError("剩余产品SKU或数量与删除前不一致，已停止保存。")
        if not after["rows"]:
            raise RuntimeError("安全护栏阻止保存空产品订单。")
        self._click_save()
        self._verify_saved_from_list(system_id, expected_remaining)
        return OrderResult(
            system_id,
            platform_id,
            "saved",
            removed_skus=[row["platformSku"] for row in targets],
            detail="已保存并通过列表复核",
        )

    def run(self, scan_only: bool = False) -> RunReport:
        report = RunReport(
            started_at=datetime.now().isoformat(timespec="seconds"),
            mode="scan-only" if scan_only else "execute",
        )
        self.start_browser()
        self.ensure_logged_in()
        self.open_pending_list(refresh=True)
        state = self._read_stable_list_state()
        report.scanned_orders = int(state.get("scanned", 0))
        candidates = state.get("candidates", [])
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in candidates:
            unique[(item.get("systemOrderId", ""), item.get("platformOrderId", ""))] = item
        candidates = list(unique.values())
        report.candidate_orders = len(candidates)
        if len(candidates) > MAX_CANDIDATES:
            raise RuntimeError(f"候选订单达到{len(candidates)}笔，超过安全上限{MAX_CANDIDATES}，已停止。")
        self.log(f"扫描到 {report.scanned_orders} 笔待处理订单，其中 {len(candidates)} 笔显示“多个”。")
        if not candidates:
            report.finished_at = datetime.now().isoformat(timespec="seconds")
            self._save_report(report)
            return report

        repeated_error = ""
        repeated_error_count = 0
        for index, candidate in enumerate(candidates, 1):
            if self.stop_requested:
                raise RuntimeError("用户已停止运行。")
            system_id = candidate["systemOrderId"]
            self.log(f"[{index}/{len(candidates)}] 检查 {system_id}")
            self.progress(index - 1, len(candidates))
            try:
                result = self.process_order(candidate, scan_only)
            except Exception as exc:
                if self.stop_requested:
                    raise RuntimeError("用户已停止运行。") from exc
                result = OrderResult(
                    system_id,
                    candidate.get("platformOrderId", ""),
                    "error",
                    detail=str(exc),
                )
                self.log(f"  异常：{exc}")
                # Do not retry uncertain state. Return to the list without clicking any form button.
                try:
                    self.open_pending_list(refresh=True)
                except Exception:
                    self._connect_page()
                    self.open_pending_list(refresh=True)
            report.results.append(result)
            if result.status == "saved":
                report.modified_orders += 1
                report.removed_rows += len(result.removed_skus)
                self.log(f"  已删除 {', '.join(result.removed_skus)} 并保存。")
            elif result.status == "skipped":
                report.skipped_orders += 1
                self.log(f"  跳过：{result.detail}")
            elif result.status == "scan":
                report.skipped_orders += 1
                self.log(f"  预检命中：{', '.join(result.removed_skus)}（未修改）")
            else:
                report.error_orders += 1
                if result.detail == repeated_error:
                    repeated_error_count += 1
                else:
                    repeated_error = result.detail
                    repeated_error_count = 1
            self.progress(index, len(candidates))
            self._save_report(report, checkpoint=True)
            if result.status == "error" and repeated_error_count >= 3:
                report.stopped_early = True
                report.stop_reason = f"连续3笔出现相同异常：{result.detail}"
                self.log(f"安全停止：{report.stop_reason}；剩余候选未继续操作。")
                break
            if result.status != "error":
                repeated_error = ""
                repeated_error_count = 0

        self.log("正在刷新待处理列表并执行最终复核……")
        self.open_pending_list(refresh=True)
        final_state = self._read_stable_list_state()
        report.remaining_multiple = len(final_state.get("candidates", []))
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        self._save_report(report)
        return report

    def _save_report(self, report: RunReport, checkpoint: bool = False) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = report.started_at.replace(":", "-")
        suffix = "-checkpoint" if checkpoint else ""
        path = REPORT_DIR / f"run-{stamp}{suffix}.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class App(tk.Tk):
    def __init__(self, scan_only_default: bool = False):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("780x590")
        self.minsize(680, 520)
        self.configure(bg="#f4f6f8")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.automation: LingxingAutomation | None = None
        self.worker: threading.Thread | None = None
        self.scan_only = tk.BooleanVar(value=scan_only_default)
        self.auto_started = False
        self._build_ui()
        self.after(100, self._drain_events)
        self.after(1000, self._auto_start)

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg="#17324d", padx=22, pady=18)
        header.pack(fill="x")
        tk.Label(header, text=APP_NAME, bg="#17324d", fg="white", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="仅清理：纯数字平台SKU + SKU/产品名称/可用库存均无匹配；绝不审核或删除订单",
            bg="#17324d",
            fg="#d8e7f3",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(6, 0))

        controls = tk.Frame(self, bg="#f4f6f8", padx=22, pady=14)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="开始自动处理", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))
        ttk.Checkbutton(controls, text="仅扫描（不修改、不保存）", variable=self.scan_only).pack(side="left", padx=(18, 0))
        ttk.Button(controls, text="打开报告目录", command=self._open_reports).pack(side="right")

        status_frame = tk.Frame(self, bg="white", padx=18, pady=14, highlightbackground="#dce3e8", highlightthickness=1)
        status_frame.pack(fill="x", padx=22)
        self.status = tk.Label(status_frame, text="准备启动……", bg="white", fg="#17324d", font=("Microsoft YaHei UI", 11, "bold"))
        self.status.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(status_frame, mode="determinate", maximum=1, value=0)
        self.progress_bar.pack(fill="x", pady=(10, 0))

        log_frame = tk.Frame(self, bg="#f4f6f8", padx=22, pady=14)
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="运行记录", bg="#f4f6f8", fg="#334e68", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.log_widget = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            bg="#101820",
            fg="#d7e3ec",
            insertbackground="white",
            font=("Consolas", 10),
            padx=12,
            pady=10,
        )
        self.log_widget.pack(fill="both", expand=True, pady=(7, 0))

        footer = tk.Label(
            self,
            text=f"报告保存在：{REPORT_DIR}",
            bg="#f4f6f8",
            fg="#627d98",
            font=("Microsoft YaHei UI", 8),
            pady=8,
        )
        footer.pack(fill="x")

    def _auto_start(self) -> None:
        if not self.auto_started:
            self.auto_started = True
            self._start()

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.configure(text="正在连接领星……")
        self.progress_bar.configure(value=0, maximum=1)
        self._clear_log()
        self.automation = LingxingAutomation(
            log=lambda text: self.events.put(("log", text)),
            progress=lambda done, total: self.events.put(("progress", (done, total))),
        )
        mode = bool(self.scan_only.get())
        self.worker = threading.Thread(target=self._run_worker, args=(mode,), daemon=True)
        self.worker.start()

    def _run_worker(self, scan_only: bool) -> None:
        try:
            assert self.automation
            report = self.automation.run(scan_only=scan_only)
            self.events.put(("done", report))
        except Exception as exc:
            self.events.put(("error", (str(exc), traceback.format_exc())))

    def _stop(self) -> None:
        if self.automation:
            self.automation.request_stop()
            self._append_log("已请求停止；程序会在当前安全检查点停止。")
            self.stop_button.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                    self.status.configure(text=payload)
                elif kind == "progress":
                    done, total = payload
                    self.progress_bar.configure(maximum=max(1, total), value=done)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(*payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_done(self, report: RunReport) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if report.mode == "scan-only":
            summary = (
                f"预检完成：扫描 {report.scanned_orders} 笔，候选 {report.candidate_orders} 笔，"
                f"命中 {sum(len(r.removed_skus) for r in report.results)} 行，异常 {report.error_orders} 笔，"
                f"复核仍显示“多个” {report.remaining_multiple} 笔；未修改任何订单。"
            )
        else:
            summary = (
                f"处理完成：扫描 {report.scanned_orders} 笔，修改 {report.modified_orders} 笔，"
                f"删除 {report.removed_rows} 行，跳过 {report.skipped_orders} 笔，"
                f"异常 {report.error_orders} 笔，复核仍显示“多个” {report.remaining_multiple} 笔。"
            )
        if report.stopped_early:
            summary += f" 已提前安全停止：{report.stop_reason}。"
        self.status.configure(text=summary)
        self._append_log(summary)
        if report.error_orders:
            messagebox.showwarning(APP_NAME, summary + "\n\n请打开报告查看异常订单，程序不会自动重复点击。")
        else:
            messagebox.showinfo(APP_NAME, summary)

    def _on_error(self, message: str, trace: str) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.configure(text="运行已安全停止")
        self._append_log(f"运行已安全停止：{message}")
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        error_path = REPORT_DIR / f"error-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        error_path.write_text(trace, encoding="utf-8")
        messagebox.showerror(APP_NAME, f"运行已安全停止：\n{message}\n\n错误日志：{error_path}")

    def _append_log(self, text: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _open_reports(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(REPORT_DIR)  # type: ignore[attr-defined]


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(APP_NAME, "程序已经在运行，请勿重复启动。")
        root.destroy()
        raise SystemExit(2)
    if "--self-test" in sys.argv[1:]:
        automation = LingxingAutomation(lambda _text: None, lambda _done, _total: None)
        automation.start_browser()
        if not automation.page or "oms.xlwms.com" not in automation.page.url():
            raise SystemExit(3)
        automation.page.close()
        raise SystemExit(0)
    App(scan_only_default="--scan-only" in sys.argv[1:]).mainloop()
