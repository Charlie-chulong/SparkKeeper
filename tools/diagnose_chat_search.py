"""只搜索并保存本地诊断；不打开好友、不编辑消息、不保存登录状态。

从仓库运行：PYTHONPATH=src python tools/diagnose_chat_search.py --help
截图与 DOM 可能包含联系人信息，请勿公开上传整个输出目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from spark_keeper.automation.browser import BrowserSessionFactory
from spark_keeper.automation.douyin_chat import DouyinChatAdapter
from spark_keeper.automation.errors import AutomationError
from spark_keeper.dpapi import DpapiJsonStore

OBSERVER = r"""() => {
    if (window.__skSearchProbe) return;
    const log = window.__skSearchProbe = {events: [], blocked: 0};
    const record = item => {
        if (log.events.length < 1000) log.events.push({ms: performance.now(), ...item});
    };
    const search = node => node instanceof HTMLInputElement
        && node.placeholder.includes('搜索') && node.dataset.e2e !== 'searchbar-input';
    for (const type of ['keydown','keyup','beforeinput','input','compositionstart','compositionend']) {
        document.addEventListener(type, event => {
            if (search(event.target)) record({type, key: event.key, inputType: event.inputType,
                value: event.target.value});
            if (event.target.matches?.('[contenteditable="true"],textarea') ||
                (type === 'keydown' && event.key === 'Enter')) {
                log.blocked++; event.preventDefault(); event.stopImmediatePropagation();
            }
        }, true);
    }
    document.addEventListener('click', event => {
        if (!search(event.target)) {
            log.blocked++; event.preventDefault(); event.stopImmediatePropagation();
        }
    }, true);
    document.addEventListener('submit', event => {
        log.blocked++; event.preventDefault(); event.stopImmediatePropagation();
    }, true);
}"""

# Structural DOM only: no HTML scripts, attributes, URLs, storage, or React state.
PANEL_DOM = r"""(input, bounds) => {
    let root = input;
    for (let node = input.parentElement; node && node !== document.body; node = node.parentElement) {
        const r = node.getBoundingClientRect();
        if (r.width > 0 && r.width <= innerWidth * .5 && r.height >= innerHeight * .35) {
            root = node; break;
        }
    }
    return {inputValue: input.value, nodes: [root, ...root.querySelectorAll('*')]
        .filter(n => !['SCRIPT','STYLE','LINK','META'].includes(n.tagName)).slice(0, 3000)
        .map(n => {
            const r = n.getBoundingClientRect(), s = getComputedStyle(n);
            return {tag: n.tagName, role: n.getAttribute('role'), children: n.children.length,
                text: [...n.childNodes].filter(c => c.nodeType === Node.TEXT_NODE)
                    .map(c => c.textContent).join('').slice(0, 300),
                visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none',
                bounds: {x:r.x,y:r.y,width:r.width,height:r.height}};
        })};
}"""


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class SearchProbe(DouyinChatAdapter):
    def __init__(self, page, output: Path) -> None:
        super().__init__(page)
        self.output = output
        self.polls: list[dict] = []
        self.started = time.monotonic()

    async def _extract_candidate_rows(self, query, search):
        rows = await super()._extract_candidate_rows(query, search)
        self.polls.append(
            {
                "ms": round((time.monotonic() - self.started) * 1000),
                "rows": len(rows),
                "leaf_counts": dict(self._last_candidate_diagnostics.get("leaf_counts", {})),
                "row_counts": dict(self._last_candidate_diagnostics.get("row_counts", {})),
            }
        )
        return rows

    async def capture(self, label: str) -> None:
        search = await self._chat_search_input()
        # Reuse panel bounds from the actual extractor, not a second search algorithm.
        bounds = self._last_candidate_diagnostics.get("panel_bounds")
        dom = await search.evaluate(PANEL_DOM, bounds)
        save_json(self.output / f"{label}-dom.json", dom)
        if bounds:
            viewport = self.page.viewport_size or {"width": 1440, "height": 1000}
            x, y = max(0, bounds["left"]), max(0, bounds["top"])
            width = min(bounds["right"], viewport["width"]) - x
            height = min(bounds["bottom"], viewport["height"]) - y
            if width > 0 and height > 0:
                await self.page.screenshot(
                    path=str(self.output / f"{label}.png"),
                    clip={"x": x, "y": y, "width": width, "height": height},
                )


async def collect(page, output: Path) -> SearchProbe:
    await page.add_init_script(f"({OBSERVER})()")
    await page.evaluate(OBSERVER)
    return SearchProbe(page, output)


async def diagnose(args) -> None:
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT display_name FROM targets WHERE id=?", (args.target_id,)
        ).fetchone()
    if row is None:
        raise ValueError("找不到指定联系人")
    query = row[0]
    output = args.output / datetime.now(UTC).astimezone().strftime("search-%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "mode": "search-only",
        "target_id": args.target_id,
        "query": query,
        "network": [],
        "observations": [],
        "capture_errors": [],
    }
    started = time.monotonic()

    def network(kind, request, status=None):
        if request.resource_type not in ("xhr", "fetch") or len(report["network"]) >= 2000:
            return
        parsed = urlsplit(request.url)
        # No query strings, headers, request/response bodies or authentication material.
        report["network"].append(
            {
                "ms": round((time.monotonic() - started) * 1000),
                "kind": kind,
                "host": parsed.hostname,
                "path": parsed.path,
                "method": request.method,
                "status": status,
            }
        )

    factory = BrowserSessionFactory(DpapiJsonStore(args.auth_state))
    async with factory.open(headless=True, use_saved_state=True) as session:
        page = session.page
        page.on("request", lambda request: network("request", request))
        page.on("response", lambda response: network("response", response.request, response.status))
        page.on("requestfailed", lambda request: network("failed", request))
        probe = await collect(page, output)
        try:
            await probe.open_chat()
            candidates = await probe.search_targets(query)
            report["candidate_count"] = len(candidates)
            report["extraction"] = probe._last_candidate_diagnostics
            report["observations"].append(
                {"phase": "production-search", "candidates": len(candidates)}
            )
            try:
                await probe.capture("search-end")
            except (PlaywrightError, AutomationError, OSError) as exc:
                report["capture_errors"].append(type(exc).__name__)
            # Observe without typing again: distinguish delayed rendering from a new submission.
            if not candidates:
                search = await probe._chat_search_input()
                for _ in range(20):
                    await page.wait_for_timeout(250)
                    rows = await probe._extract_candidate_rows(query, search)
                    if rows:
                        break
                report["observations"].append(
                    {"phase": "extra-wait-no-input", "candidates": len(rows)}
                )
                try:
                    await probe.capture("extra-wait-end")
                except (PlaywrightError, AutomationError, OSError) as exc:
                    report["capture_errors"].append(type(exc).__name__)
        except (PlaywrightError, AutomationError) as exc:
            report["error_type"] = type(exc).__name__
        finally:
            report["polls"] = probe.polls
            try:
                report["input_events"] = await page.evaluate("window.__skSearchProbe")
            except PlaywrightError as exc:
                report["capture_errors"].append(type(exc).__name__)
            save_json(output / "report.json", report)
    print(
        json.dumps(
            {
                "output": str(output),
                "candidate_count": report.get("candidate_count"),
                "observations": report["observations"],
                "error_type": report.get("error_type"),
            },
            ensure_ascii=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--auth-state", type=Path, required=True)
    parser.add_argument("--target-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(diagnose(parser.parse_args()))


if __name__ == "__main__":
    main()
