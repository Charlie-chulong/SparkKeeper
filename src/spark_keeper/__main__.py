from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys


async def _run_browser_smoke() -> None:
    from .automation.browser import BrowserSessionFactory
    from .dpapi import DpapiJsonStore
    from .paths import AppPaths

    paths = AppPaths.discover()
    paths.ensure_runtime_dirs()
    factory = BrowserSessionFactory(DpapiJsonStore(paths.auth_state))
    async with factory.open(headless=True, use_saved_state=False) as session:
        await session.page.set_content("<title>SparkKeeper browser smoke</title>")
        if await session.page.title() != "SparkKeeper browser smoke":
            raise RuntimeError("Chromium 冒烟验证失败")


def _show_startup_error(error: Exception, *, smoke_mode: bool) -> None:
    detail = (
        "程序未能安全启动，未继续执行任何任务。\n\n"
        f"{error}\n\n"
        "请保留当前数据和备份；若提示数据版本较新，请使用对应的新版本，"
        "不要删除数据库或覆盖恢复旧备份。"
    )
    if sys.stderr is not None:
        print(detail, file=sys.stderr)
    if smoke_mode:
        return
    from . import APP_NAME

    if sys.platform == "win32":
        import ctypes

        message_box = ctypes.windll.user32.MessageBoxW
        message_box.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint
        ]
        message_box.restype = ctypes.c_int
        message_box(None, detail, f"{APP_NAME} · 启动失败", 0x10)
    else:
        from PySide6.QtWidgets import QApplication, QMessageBox

        _application = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, f"{APP_NAME} · 启动失败", detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="续火花助手")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--smoke", action="store_true", help="使用临时数据启动独立界面后自动关闭，不执行网页任务")
    modes.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--browser-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.scheduled:
        from .worker import run_scheduled

        return asyncio.run(run_scheduled())
    if args.browser_smoke:
        asyncio.run(_run_browser_smoke())
        return 0

    try:
        if sys.platform == "win32":
            import ctypes

            # Keep the GUI separate from Python's taskbar group, including source launches.
            set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            set_app_id.argtypes = [ctypes.c_wchar_p]
            set_app_id.restype = ctypes.c_long
            result = set_app_id("SparkKeeper.Local")
            if result < 0:
                raise OSError(result, "无法设置续火花助手任务栏标识")

        from .ui.app import run

        return run(smoke_mode=args.smoke)
    except (RuntimeError, OSError, ValueError, ImportError, sqlite3.Error) as exc:
        _show_startup_error(exc, smoke_mode=args.smoke)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
