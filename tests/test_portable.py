from __future__ import annotations

import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import spark_keeper.automation.browser as browser_module
import spark_keeper.paths as paths_module
import spark_keeper.scheduler as scheduler_module
import spark_keeper.worker as worker_module
from spark_keeper.__main__ import main
from spark_keeper.automation.errors import AutomationError
from spark_keeper.paths import AppPaths
from spark_keeper.scheduler import TASK_XML_NAMESPACE, build_task_xml

build_portable = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools" / "build_portable.py")
)
reject_sensitive_files = build_portable["reject_sensitive_files"]
copy_licenses = build_portable["copy_licenses"]
copy_qt_licenses = build_portable["copy_qt_licenses"]
trim_unused_qt_plugins = build_portable["trim_unused_qt_plugins"]


def test_frozen_paths_keep_runtime_data_outside_install_directory(tmp_path, monkeypatch) -> None:
    install = tmp_path / "portable"
    local_app_data = tmp_path / "local-app-data"
    executable = install / "SparkKeeper.exe"
    monkeypatch.delenv("SPARK_KEEPER_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(paths_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths_module.sys, "executable", str(executable))

    paths = AppPaths.discover()

    assert paths.root == install.resolve()
    assert paths.data == local_app_data / "SparkKeeper" / "data"
    assert paths.database == paths.data / "spark-keeper.sqlite3"
    assert paths.auth_state == paths.data / "auth-state.bin"
    assert paths.scheduler_xml == local_app_data / "SparkKeeper" / "spark-keeper-task.xml"
    assert install not in paths.database.parents


def test_configured_root_keeps_development_layout_when_frozen(tmp_path, monkeypatch) -> None:
    root = tmp_path / "isolated-root"
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(root))
    monkeypatch.setattr(paths_module.sys, "frozen", True, raising=False)

    paths = AppPaths.discover()

    assert paths.root == root.resolve()
    assert paths.data == root.resolve() / "work" / "local-data"


def test_frozen_browser_uses_bundled_playwright_browsers(tmp_path, monkeypatch) -> None:
    install = tmp_path / "portable"
    browsers = install / "browsers"
    browsers.mkdir(parents=True)
    monkeypatch.setattr(browser_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        browser_module.sys,
        "executable",
        str(install / "SparkKeeper.exe"),
    )
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "external-browser-cache"))

    browser_module._configure_bundled_browser_path()

    assert browser_module.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)


def test_frozen_browser_rejects_incomplete_archive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(browser_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        browser_module.sys,
        "executable",
        str(tmp_path / "SparkKeeper.exe"),
    )

    with pytest.raises(AutomationError, match="缺少 Chromium"):
        browser_module._configure_bundled_browser_path()


def test_frozen_scheduler_uses_executable_entrypoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module.sys, "frozen", True, raising=False)
    xml = build_task_xml(
        send_time="21:00",
        python_executable=tmp_path / "SparkKeeper.exe",
        working_directory=tmp_path,
        user_sid="S-1-5-21-1234",
        arguments=scheduler_module._scheduled_task_arguments(),
    )
    document = ET.fromstring(xml)
    namespace = {"t": TASK_XML_NAMESPACE}
    assert document.findtext(".//t:Arguments", namespaces=namespace) == "--scheduled"


def test_packaged_scheduled_entrypoint_calls_worker(monkeypatch) -> None:
    called = False

    async def fake_run_scheduled() -> int:
        nonlocal called
        called = True
        return 7

    monkeypatch.setattr(worker_module, "run_scheduled", fake_run_scheduled)

    assert main(["--scheduled"]) == 7
    assert called


def test_release_audit_rejects_runtime_data(tmp_path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "SparkKeeper.exe").write_bytes(b"safe")
    reject_sensitive_files(release)

    (release / "auth-state.bin").write_bytes(b"must-not-ship")
    with pytest.raises(RuntimeError, match="发行目录含运行数据"):
        reject_sensitive_files(release)


def test_license_copy_preserves_project_and_third_party_terms_in_manifest(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    project_license = root / "LICENSE"
    project_license.write_bytes(
        b"Project license\r\nRequired Notice: Copyright 2026 Example contributors\r\n"
    )
    python_license = root / "LICENSE.txt"
    python_license.write_bytes(b"Python license\r\n")
    playwright_license = (
        root / "Lib" / "site-packages" / "playwright" / "driver" / "LICENSE"
    )
    playwright_license.parent.mkdir(parents=True)
    playwright_license.write_bytes(b"Playwright license\n")
    dependency_license = root / "dependency-LICENSE.txt"
    dependency_license.write_bytes(b"Dependency license\n")
    inno_license = root / "tools" / "installer" / "LICENSE.txt"
    inno_license.parent.mkdir(parents=True)
    inno_license.write_bytes(b"Unmodified Inno Setup translation license\r\n")
    qt_source = root / "third_party" / "qt-6.11.2"
    qt_source.mkdir(parents=True)
    for filename in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
        (qt_source / filename).write_bytes(b"Unmodified Qt license\n")
    (qt_source / "manifest.json").write_text('{"version":"6.11.2"}', encoding="utf-8")
    monkeypatch.setitem(copy_licenses.__globals__, "ROOT", root)
    monkeypatch.setitem(copy_licenses.__globals__, "VENV", root)
    monkeypatch.setitem(copy_licenses.__globals__, "sys", SimpleNamespace(base_prefix=root))
    monkeypatch.setitem(
        copy_licenses.__globals__, "distribution_license", lambda *_args: dependency_license
    )
    monkeypatch.setitem(
        copy_licenses.__globals__, "distribution", lambda _name: SimpleNamespace(version="6.11.2")
    )

    release = tmp_path / "release"
    copy_licenses(release)
    expected = {
        "LICENSE": project_license,
        "licenses/Python-LICENSE.txt": python_license,
        "licenses/Playwright-LICENSE.txt": playwright_license,
        "licenses/Windows-Toasts-LICENSE.txt": dependency_license,
        "licenses/PyInstaller-COPYING.txt": dependency_license,
        "licenses/InnoSetup/LICENSE.txt": inno_license,
        "licenses/Qt/LGPL-3.0-only.txt": qt_source / "LGPL-3.0-only.txt",
        "licenses/Qt/GPL-3.0-only.txt": qt_source / "GPL-3.0-only.txt",
        "licenses/Qt/manifest.json": qt_source / "manifest.json",
    }
    manifest_path = build_portable["write_release_manifest"](release, "3.0.0")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, source in expected.items():
        assert (release / name).read_bytes() == source.read_bytes()
        assert manifest["files"][name] == build_portable["sha256_file"](source)
    inno_license.unlink()
    with pytest.raises(FileNotFoundError, match="LICENSE.txt"):
        copy_licenses(tmp_path / "missing-inno-license-release")


def test_license_copy_rejects_missing_project_license(tmp_path, monkeypatch):
    monkeypatch.setitem(copy_licenses.__globals__, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        copy_licenses(tmp_path / "release")


def test_qt_license_copy_requires_matching_version_and_preserves_notices(tmp_path, monkeypatch):
    monkeypatch.setitem(copy_qt_licenses.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(
        copy_qt_licenses.__globals__,
        "distribution",
        lambda _name: SimpleNamespace(version="6.11.2"),
    )
    source = tmp_path / "third_party" / "qt-6.11.2"
    source.mkdir(parents=True)
    (source / "LGPL-3.0-only.txt").write_text("LGPL fixture", encoding="utf-8")
    (source / "GPL-3.0-only.txt").write_text("GPL fixture", encoding="utf-8")
    manifest = source / "manifest.json"
    manifest.write_text(json.dumps({"version": "6.11.1"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="版本"):
        copy_qt_licenses(tmp_path / "licenses")
    manifest.write_text(json.dumps({"version": "6.11.2"}), encoding="utf-8")
    notice = source / "component" / "NOTICE.txt"
    notice.parent.mkdir()
    notice.write_text("Original copyright notice", encoding="utf-8")
    copy_qt_licenses(tmp_path / "licenses")
    assert (
        tmp_path / "licenses" / "Qt" / "component" / "NOTICE.txt"
    ).read_bytes() == notice.read_bytes()
    assert (tmp_path / "licenses" / "Qt" / "manifest.json").read_bytes() == manifest.read_bytes()


@pytest.mark.parametrize("missing", ["LGPL-3.0-only.txt", "GPL-3.0-only.txt", "manifest.json"])
def test_qt_license_copy_rejects_incomplete_bundle(tmp_path, monkeypatch, missing):
    monkeypatch.setitem(copy_qt_licenses.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(
        copy_qt_licenses.__globals__,
        "distribution",
        lambda _name: SimpleNamespace(version="6.11.2"),
    )
    source = tmp_path / "third_party" / "qt-6.11.2"
    source.mkdir(parents=True)
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "manifest.json"):
        if name != missing:
            (source / name).write_text('{"version":"6.11.2"}', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="缺少 Qt"):
        copy_qt_licenses(tmp_path / "licenses")


def test_qt_plugin_trim_keeps_platform_and_core_but_removes_unused_formats(tmp_path):
    qt = tmp_path / "_internal" / "PySide6"
    paths = [
        qt / "plugins" / "platforms" / "qwindows.dll",
        qt / "plugins" / "imageformats" / "qpdf.dll",
        qt / "plugins" / "iconengines" / "qsvgicon.dll",
        qt / "Qt6Pdf.dll",
        qt / "Qt6Svg.dll",
        qt / "Qt6Gui.dll",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"binary fixture")
    trim_unused_qt_plugins(tmp_path)
    assert paths[0].is_file()
    assert paths[-1].is_file()
    assert all(not path.exists() for path in paths[1:-1])


def test_qt_plugin_trim_rejects_missing_windows_platform(tmp_path):
    with pytest.raises(RuntimeError, match="Qt Windows"):
        trim_unused_qt_plugins(tmp_path)
