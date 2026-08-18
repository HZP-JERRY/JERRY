from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

import websocket


APP_NAME = "领星数字SKU清理助手"
APP_VERSION = "1.0.0"
LIST_URL = "https://oms.xlwms.com/platform/order/list"
LOGIN_URL_PART = "/login"
DEBUG_PORT = 19225
MAX_CANDIDATES = 200


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
                continue
            if "error" in response:
                raise CDPError(f"{method}: {response['error'].get('message', response['error'])}")
            return response.get("result", {})

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
        self._wait(lambda: "待处理" in self.page.body_text(), 15, "待处理订单")
        self._set_page_size_100()
        self._wait(lambda: bool(self._read_list_state().get("ready")), 25, "订单表格")

    def _set_page_size_100(self) -> None:
        assert self.page
        if "100条/页" in self.page.body_text():
            return
        opened = self.page.evaluate(
            """(() => {
              const inputs=[...document.querySelectorAll('input')];
              const input=inputs.find(i => /条\\/页/.test(i.value||i.placeholder||''));
              if(!input) return false;
              (input.closest('.el-select')||input).click();
              return true;
            })()"""
        )
        if not opened:
            self.log("未找到分页数量选择器，将按当前页面数量继续。")
            return
        time.sleep(0.5)
        selected = self.page.evaluate(
            """(() => {
              const options=[...document.querySelectorAll('.el-select-dropdown__item')]
                .filter(e => e.offsetParent!==null);
              const option=options.find(e => (e.innerText||'').trim()==='100条/页');
              if(!option) return false;
              option.click(); return true;
            })()"""
        )
        if selected:
            time.sleep(1)

    def _read_list_state(self) -> dict[str, Any]:
        assert self.page
        script = """(() => {
          const headers=[...document.querySelectorAll('table.vxe-table--header th')]
            .map(th=>({text:(th.innerText||'').replace(/\\s+/g,'').trim(), colid:th.getAttribute('colid')}));
          const col=(name)=>headers.find(h=>h.text===name)?.colid;
          const platformCol=col('平台单号');
          const skuCol=col('平台SKU*数量');
          if(!platformCol||!skuCol) return {ready:false, scanned:0, candidates:[]};
          const rows=[...document.querySelectorAll('table.vxe-table--body tr[rowid]')];
          const rowIds=[...new Set(rows.map(r=>r.getAttribute('rowid')).filter(Boolean))];
          const candidates=[];
          for(const rowid of rowIds){
            const main=rows.find(r=>r.getAttribute('rowid')===rowid && r.querySelector(`td[colid="${skuCol}"]`));
            if(!main) continue;
            const skuText=(main.querySelector(`td[colid="${skuCol}"]`)?.innerText||'').trim();
            if(!skuText.includes('多个')) continue;
            const platformId=(main.querySelector(`td[colid="${platformCol}"]`)?.innerText||'').trim().split(/\\s+/)[0];
            candidates.push({systemOrderId:rowid, platformOrderId:platformId, platformSkuText:skuText});
          }
          return {ready:true, scanned:rowIds.length, candidates};
        })()"""
        result = self.page.evaluate(script)
        return result if isinstance(result, dict) else {"ready": False, "scanned": 0, "candidates": []}

    def _click_edit(self, system_order_id: str) -> None:
        assert self.page
        clicked = self.page.evaluate(
            f"""(() => {{
              const safe={json.dumps(system_order_id)};
              const rows=[...document.querySelectorAll('table.vxe-table--body tr[rowid]')]
                .filter(r=>r.getAttribute('rowid')===safe);
              const row=rows.find(r=>[...r.querySelectorAll('button')]
                .some(b=>(b.innerText||'').trim()==='编辑'));
              const button=row && [...row.querySelectorAll('button')]
                .find(b=>(b.innerText||'').trim()==='编辑');
              if(!button) return false;
              button.click(); return true;
            }})()"""
        )
        if not clicked:
            raise RuntimeError("找不到该订单的“编辑”按钮。")
        self._wait(lambda: f"/platform/order/edit/{system_order_id}" in self.page.url(), 20, "订单编辑页")
        self._wait(lambda: "产品信息" in self.page.body_text(), 20, "产品信息")

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

    def _remove_row(self, row_id: str) -> None:
        assert self.page
        before_count = len(self._read_edit_state()["rows"])
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
        time.sleep(0.35)
        confirmed = self.page.evaluate(
            """(() => {
              const dialogs=[...document.querySelectorAll('.el-message-box__wrapper,.el-dialog__wrapper')]
                .filter(e=>e.offsetParent!==null);
              if(!dialogs.length) return 'none';
              const dialog=dialogs[dialogs.length-1];
              const buttons=[...dialog.querySelectorAll('button')].filter(b=>b.offsetParent!==null);
              const confirm=buttons.find(b=>['确定','确认'].includes((b.innerText||'').trim()));
              if(!confirm) return false;
              confirm.click(); return true;
            })()"""
        )
        if confirmed is False:
            raise RuntimeError("移除确认框中未找到“确定/确认”按钮，已停止保存。")
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
              const buttons=[...document.querySelectorAll('button')]
                .filter(b=>b.offsetParent!==null && (b.innerText||'').trim()==='保存');
              if(buttons.length!==1) return false;
              buttons[0].click(); return true;
            })()"""
        )
        if not clicked:
            raise RuntimeError("“保存”按钮不是唯一精确匹配，已停止以避免误操作。")
        self._wait(lambda: "/platform/order/list" in self.page.url(), 25, "保存后返回订单列表")

    def process_order(self, candidate: dict[str, Any], scan_only: bool) -> OrderResult:
        system_id = candidate["systemOrderId"]
        platform_id = candidate.get("platformOrderId", "")
        self._click_edit(system_id)
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
        return OrderResult(
            system_id,
            platform_id,
            "saved",
            removed_skus=[row["platformSku"] for row in targets],
            detail="已保存并返回订单列表",
        )

    def run(self, scan_only: bool = False) -> RunReport:
        report = RunReport(
            started_at=datetime.now().isoformat(timespec="seconds"),
            mode="scan-only" if scan_only else "execute",
        )
        self.start_browser()
        self.ensure_logged_in()
        self.open_pending_list(refresh=True)
        state = self._read_list_state()
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

        for index, candidate in enumerate(candidates, 1):
            if self.stop_requested:
                raise RuntimeError("用户已停止运行。")
            system_id = candidate["systemOrderId"]
            self.log(f"[{index}/{len(candidates)}] 检查 {system_id}")
            self.progress(index - 1, len(candidates))
            try:
                result = self.process_order(candidate, scan_only)
            except Exception as exc:
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
            self.progress(index, len(candidates))
            self._save_report(report, checkpoint=True)

        self.log("正在刷新待处理列表并执行最终复核……")
        self.open_pending_list(refresh=True)
        final_state = self._read_list_state()
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
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("780x590")
        self.minsize(680, 520)
        self.configure(bg="#f4f6f8")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.automation: LingxingAutomation | None = None
        self.worker: threading.Thread | None = None
        self.scan_only = tk.BooleanVar(value=False)
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
                f"命中 {sum(len(r.removed_skus) for r in report.results)} 行；未修改任何订单。"
            )
        else:
            summary = (
                f"处理完成：扫描 {report.scanned_orders} 笔，修改 {report.modified_orders} 笔，"
                f"删除 {report.removed_rows} 行，跳过 {report.skipped_orders} 笔，"
                f"异常 {report.error_orders} 笔，复核仍显示“多个” {report.remaining_multiple} 笔。"
            )
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
    App().mainloop()
