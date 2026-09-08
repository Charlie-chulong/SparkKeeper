from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import distribution, distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / "Scripts" / "python.exe"
PLAYWRIGHT = VENV / "Scripts" / "playwright.exe"
LOCAL_BROWSERS = (
    VENV / "Lib" / "site-packages" / "playwright" / "driver" / "package" / ".local-browsers"
)


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def validate_version(version: str) -> str:
    """Accept canonical PEP 440 final/rc releases, never development builds."""
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:rc([1-9]\d*))?", version):
        raise ValueError(f"发行版本须为规范的 X.Y.Z 或 X.Y.ZrcN（N>=1）：{version!r}")
    return version


def release_version(version: str) -> str:
    """Derive public ZIP/tag spelling from the sole PEP 440 version source."""
    return validate_version(version).replace("rc", "-rc.")


def project_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "src" / "spark_keeper" / "__init__.py").read_text("utf-8"))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    ]
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or not isinstance(values[0].value, str):
        raise ValueError("__version__ 必须是唯一的字符串常量")
    return validate_version(values[0].value)


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def is_sensitive_path(name: str) -> bool:
    parts = name.replace("\\", "/").casefold().split("/")
    filename = parts[-1]
    return (
        bool(set(parts) & {"local-data", "outputs", "backups", ".links", "__dirlock", ".venv"})
        or filename in {"auth-state.bin", "spark-keeper-task.xml", ".env"}
        or filename.startswith(".env.")
        or filename.endswith((".sqlite", ".sqlite3", ".db", ".sqlite3-wal", ".sqlite3-shm"))
    )


def portable_command(root: Path, python: Path) -> list[str]:
    build_root = root / "work" / "pyinstaller"
    return [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--onedir", "--name", "SparkKeeper",
        "--icon", str(root / "src" / "spark_keeper" / "assets" / "app-icon.ico"),
        "--paths", str(root / "src"),
        "--add-data", f"{root / 'src' / 'spark_keeper' / 'assets'}{os.pathsep}spark_keeper/assets",
        "--distpath", str(build_root / "dist"), "--workpath", str(build_root / "build"),
        "--specpath", str(build_root),
        "--collect-all", "playwright", "--collect-all", "windows_toasts",
        "--exclude-module", "pytest", "--exclude-module", "ruff",
        "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtPdfWidgets", "--exclude-module", "PySide6.QtSvg",
        "--exclude-module", "PySide6.QtSvgWidgets", "--exclude-module", "PySide6.QtTest",
        str(root / "tools" / "portable_entry.py"),
    ]


def portable_configuration(root: Path) -> dict:
    # Normalize locations, not options: relocating a checkout is not a source change.
    command = portable_command(root, root / ".venv" / "Scripts" / "python.exe")
    return {
        "command": [argument.replace(str(root), "${ROOT}") for argument in command],
        "environment": {"PLAYWRIGHT_BROWSERS_PATH": "0"},
        "browser_install": ["install", "chromium"],
    }


def source_snapshot(root: Path, qt_version: str) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+", qt_version):
        raise ValueError("构建来源 Qt 版本无效")
    names = {
        "pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.txt",
        "tools/build_portable.py", "tools/portable_entry.py",
        "tools/update.cmd", "tools/update.ps1", "tools/installer/LICENSE.txt",
    }
    # These are exactly the project trees consumed by PyInstaller/copy_licenses.
    # Never scan the checkout root (outputs, caches and user data are not inputs).
    for directory in (root / "src" / "spark_keeper", root / "third_party" / f"qt-{qt_version}"):
        if not directory.is_dir():
            raise FileNotFoundError(f"缺少构建来源目录：{directory}")
        if directory.is_symlink() or directory.is_junction() or directory.parent.is_symlink() or directory.parent.is_junction():
            raise ValueError(f"构建来源目录不能是链接：{directory}")
        for parent, directories, filenames in os.walk(directory, followlinks=False):
            directories[:] = sorted(name for name in directories if name != "__pycache__")
            for name in [*directories, *filenames]:
                path = Path(parent) / name
                if path.is_symlink() or path.is_junction():
                    raise ValueError(f"构建来源不能是链接：{path}")
            for name in filenames:
                path = Path(parent) / name
                relative = path.relative_to(root)
                packaged = (relative.parts[0] == "third_party"
                            or relative.parts[:3] == ("src", "spark_keeper", "assets")
                            or path.suffix.lower() in {".py", ".pyd", ".dll"})
                if packaged and path.suffix.lower() not in {".pyc", ".pyo"}:
                    names.add(relative.as_posix())
    files = {}
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ValueError(f"构建来源必须是普通文件：{name}")
        files[name] = sha256_file(path)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": digest, "files": files}


def build_provenance(root: Path) -> dict:
    qt_version = distribution("PySide6-Essentials").version
    return {
        "format": 1,
        "qt_version": qt_version,
        "source": source_snapshot(root, qt_version),
        "configuration": portable_configuration(root),
        "toolchain": {
            "python": sys.version,
            "platform": platform.platform(),
            "distributions": dict(sorted(
                (package.metadata["Name"], package.version) for package in distributions()
            )),
        },
    }


def validate_provenance(provenance: object, *, root: Path) -> dict:
    if not isinstance(provenance, dict) or type(provenance.get("format")) is not int or provenance.get("format") != 1:
        raise RuntimeError("缺少有效构建来源；请重新构建便携包")
    qt_version = provenance.get("qt_version")
    toolchain = provenance.get("toolchain")
    if (not isinstance(qt_version, str) or not isinstance(toolchain, dict)
            or not isinstance(toolchain.get("python"), str) or not toolchain["python"]
            or not isinstance(toolchain.get("platform"), str) or not toolchain["platform"]
            or not isinstance(toolchain.get("distributions"), dict)
            or not all(isinstance(name, str) and isinstance(version, str) and name and version
                       for name, version in toolchain["distributions"].items())
            or not {"pyinstaller", "playwright", "pyside6-essentials", "windows-toasts"} <= {
                name.lower().replace("_", "-") for name in toolchain["distributions"]
            }):
        raise RuntimeError("构建来源缺少依赖或构建工具版本")
    if (provenance.get("source") != source_snapshot(root, qt_version)
            or provenance.get("configuration") != portable_configuration(root)):
        raise RuntimeError("构建来源与当前业务源码、资源或构建配置不一致；请重新构建便携包")
    return provenance


def verify_build_unchanged(provenance: dict, *, root: Path) -> None:
    if build_provenance(root) != provenance:
        raise RuntimeError("构建期间源码、配置或依赖发生变化；拒绝生成发行清单")


def write_release_manifest(release_directory: Path, version: str, *, provenance: dict) -> Path:
    reject_sensitive_files(release_directory)
    files = {
        path.relative_to(release_directory).as_posix(): sha256_file(path)
        for path in sorted(release_directory.rglob("*"))
        if path.is_file() and path != release_directory / "release-manifest.json"
    }
    destination = release_directory / "release-manifest.json"
    destination.write_text(
        json.dumps(
            {"format": 1, "product": "SparkKeeper", "version": validate_version(version),
             "files": files, "build_provenance": provenance},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return destination


def write_user_guide(destination: Path, version: str) -> None:
    destination.write_text(
        f"""续火花助手 Windows 本地自用版 {version}

使用前提
1. 仅操作本人账号，并只向明确同意接收测试消息的好友发送。
2. 软件通过正常抖音网页操作，不绕过验证码、安全验证或平台风控。
3. 本地自用版未进行代码签名，Windows SmartScreen 可能显示未知发布者提示。

开始使用
1. 将整个文件夹解压到固定位置，例如 D:\\SparkKeeper；不要直接在压缩包内运行。
2. 双击 SparkKeeper.exe。
3. 点击“扫码登录”，由本人完成扫码和可能出现的安全验证。
4. 逐个添加好友，或在“好友管理”点击“扫描火花好友（不发送）”，预览并勾选后导入。
5. 选择文本或“续火花（原生表情）”，设置每日时间与好友间随机等待范围，确认完整预览后再启用每日计划。

重要说明
- 原生“续火花”与文本互斥发送，不是同名文字或小表情“[续火花吧]”。预览图不是发送结果；请先“验证配置（不发送）”检查网页表情可用性。
- 原生表情点击即发送，程序只触发一次；表情缺失或身份无法确认时拒绝发送，不自动退回文本。切换内容类型仍受同一天防重复限制；补跑使用原计划快照。
- 软件不设固定好友数量上限；每批按顺序处理启用好友，停用好友不参与发送。
- 已保存好友点击任意列选中整行，支持 Ctrl/Shift 多选；“全选”在全部选中后变为“取消选择”。选择后从“操作”菜单启用、停用或删除所选；菜单仅显示适用动作，部分选择时也可从菜单取消选择。选择本身不启用或发送。
- 删除所选会先显示人数和名单供确认；有历史的好友仅停用并保留记录，其余删除，批量操作失败整组回滚。
- 火花扫描只读当前网页可加载的聊天列表，导入仅要求可靠单聊身份，不以火花状态限制人工选择。
- 扫描中断或没有明确末端证据时显示部分结果，不保证覆盖全部联系人；可以取消扫描。
- 批量导入不发送消息，新好友默认停用，已有好友保持原状态；请核对并只启用获授权好友，再验证配置。
- 默认勾选身份可靠的新有效火花（含GRAY）和RECOVER好友；待恢复仍可独立筛选。扫描后可任意勾选或取消，新增好友仍停用。
- 账号或登录状态变化后必须重新扫描；选中群聊或身份未确认/冲突对象时会明确阻止导入，不会悄悄跳过。
- Setup 原位升级：先等待发送、扫描、登录及独立计划任务正常结束，退出全部旧程序和浏览器；不要强杀进程。双击可信新版 Setup，沿用原安装目录，无需先卸载。
- 3.0.3 Setup 自动暂停经严格归属核对的本安装 Windows 计划，程序文件完整校验通过后恢复原 Enabled；原停用保持、无任务不新建，不修改业务计划、不自动补跑。普通原位升级无需先手动停用或为恢复任务重新保存计划。
- 安装取消仅在未改程序文件或验证原文件完整时安全恢复计划；部分更新、崩溃、校验或任务恢复失败保留 maintenance\\pending.json 并保持暂停。请重跑原目录同版或更新版 Setup 修复，不要删维护记录、手动启用任务或混合新旧文件绕过保护。旧版不认识维护互斥，安装期间不要启动旧副本。
- 首次从 ZIP 迁入不同安装目录：先在旧版手动停用并保存计划、正常结束任务并退出。异路径或归属不明的已启用同名任务会阻止安装，不自动绑定；迁入后在新版核对配置、按需启用并重新保存计划，更新快捷方式。
- ZIP 便携更新仍须先手动停用并保存每日计划，等待任务结束并正常退出。新版 ZIP 解压到旧目录之外，双击包根目录“更新.cmd”并选择原便携目录；整目录替换，不逐文件覆盖、不只替换 EXE，也不要用 ZIP 覆盖安装器管理的目录。
- ZIP 更新器验证文件 SHA256，保留旧程序旁路备份；不会自动暂停/恢复计划、强杀进程或自动回滚数据库。校验值只能防损坏，不能代替可信下载来源。首次从 dev18 迁移须明确确认旧目录；更新后核对版本、好友和配置，再人工启用并保存计划。
- 已迁移数据库不得直接用旧程序/旧快照覆盖回退。设置了 SPARK_KEEPER_ROOT 时拒绝正式 Setup 安装及 ZIP 更新；正式用户数据外置于 AppData，不放在程序目录。
- 默认在两次实际发送尝试之间随机等待 3–8 秒，可在任务台设置 0–120 秒；0–0 表示关闭。
- 随机等待只能降低连续发送频率，不能保证避免平台限制或封禁。
- 事件按用户要求保留真实对象、完整错误、原因链和调用栈，不再脱敏；请在“运行日志”的完整原文区域查看并妥善保管。
- 登录态和数据库只保存在当前 Windows 用户的 %LOCALAPPDATA%\\SparkKeeper 中，登录态使用 DPAPI 保护。
- 压缩包不包含制作者的账号、好友、消息、数据库、登录态或任务 XML。
- 界面使用 PySide6 / Qt Widgets；相关开源许可与来源见 licenses\\Qt 和 THIRD_PARTY_NOTICES.txt。
- 启用每日计划后不要移动或重命名解压文件夹；如需移动，先在软件中停用计划，移动后再启用。
- 发送触发后不会自动重试；无法确认的结果会记录为 unknown，并阻止当天自动重发。
- 好友查找只在首轮搜索结果为空时重新提交同一查询一次；身份核验仍严格，这不是发送重试。
- 同一天重复发送默认拦截，只有桌面程序中的人工二次确认才能覆盖。
- 若断网、登录失效或出现安全验证，定时任务会停止且不会继续发送。

安装版卸载（不可逆清理）
1. 等待全部任务正常结束、退出所有副本，从 Windows“已安装的应用”卸载 SparkKeeper。明确确认后删除本安装程序、严格归属本安装且未被另改的 Windows 计划，以及本用户 %LOCALAPPDATA%\\SparkKeeper 全部共享数据（数据库、登录态、好友、配置、历史、防重复记录、日志、诊断、备份、维护记录等），不提供保留选项。
2. 同用户 ZIP 便携版也共享这份数据，清理会一并影响它们。另一副本运行、异路径/归属不明同名任务（即使已停用）或其他关联计划仍引用共享数据时会中止；不擅改其他任务、不强杀进程。
3. 交互卸载必须确认；静默卸载须显式 /PURGEUSERDATA（不带值）。正常安装、原位升级及取消卸载不删除用户数据。开始清理后的失败不承诺恢复已删数据，也不会恢复计划。
4. 卸载失败请保留维护记录与错误信息，优先重跑原目录中保留的卸载器并再次确认，静默仍须 /PURGEUSERDATA。仅原卸载器损坏或无法使用时用同目录同版或更新版 Setup 兜底修复，继续此前未完成的授权全清，不重建旧任务、不启动程序；按完成页提示再次卸载修复后的程序。
5. 修复不能找回已删除的数据；重新使用须重新登录和配置，历史防重复依据已清除，发送前必须人工核对。

ZIP 便携版手动卸载（不适用于安装器管理的目录）
1. 在便携版停用并保存每日计划，核对属于该便携目录的 Windows 任务已移除，等待任务结束并正常退出；不要误删其他副本的任务。
2. 删除该便携解压目录。
3. 如需手动清除登录态、配置及历史，可删除 %LOCALAPPDATA%\\SparkKeeper；这是不可逆清理，安装版和同用户其他便携副本也共享该目录，都会受影响。先退出所有副本并核对没有计划引用共享数据，不要用此步骤绕过未完成的安装维护记录。

本版本仅供本人小规模测试，不提供平台风控绕过或批量营销能力。
""",
        encoding="utf-8-sig",
    )


def distribution_license(distribution_name: str, suffix: str) -> Path:
    package = distribution(distribution_name)
    normalized_suffix = suffix.replace("\\", "/").casefold()
    matches = [
        file
        for file in package.files or ()
        if str(file).replace("\\", "/").casefold().endswith(normalized_suffix)
    ]
    if len(matches) != 1:
        raise FileNotFoundError(f"{distribution_name} 许可文件匹配数量异常：{len(matches)}")
    return Path(package.locate_file(matches[0]))


def copy_licenses(release_directory: Path) -> None:
    destination = release_directory / "licenses"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", release_directory / "LICENSE")
    sources = {
        "Python-LICENSE.txt": Path(sys.base_prefix) / "LICENSE.txt",
        "InnoSetup/LICENSE.txt": ROOT / "tools" / "installer" / "LICENSE.txt",
        "Playwright-LICENSE.txt": (
            VENV / "Lib" / "site-packages" / "playwright" / "driver" / "LICENSE"
        ),
        "Windows-Toasts-LICENSE.txt": distribution_license(
            "Windows-Toasts",
            "licenses/LICENSE",
        ),
        "PyInstaller-COPYING.txt": distribution_license(
            "pyinstaller",
            "licenses/COPYING.txt",
        ),
    }
    for name, source in sources.items():
        if not source.is_file():
            raise FileNotFoundError(f"缺少许可文件：{source}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    copy_qt_licenses(destination)


def copy_qt_licenses(destination: Path) -> None:
    qt_version = distribution("PySide6-Essentials").version
    source = ROOT / "third_party" / f"qt-{qt_version}"
    for filename in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "manifest.json"):
        if not (source / filename).is_file():
            raise FileNotFoundError(f"缺少 Qt {qt_version} 许可资料：{filename}")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != qt_version:
        raise RuntimeError("Qt 许可资料版本与已安装运行库不一致")
    shutil.copytree(source, destination / "Qt")


def trim_unused_qt_plugins(release_directory: Path) -> None:
    """The UI uses QtGui's built-in PNG decoder, not external image/SVG/PDF plugins."""
    internal = release_directory / "_internal"
    qt = internal / "PySide6"
    if not (qt / "plugins" / "platforms" / "qwindows.dll").is_file():
        raise RuntimeError("发行目录缺少 Qt Windows 平台插件")
    for name in ("imageformats", "iconengines"):
        path = qt / "plugins" / name
        if path.exists():
            shutil.rmtree(path)
    for pattern in ("Qt6Pdf*.dll", "Qt6Svg*.dll"):
        for path in internal.rglob(pattern):
            path.unlink()


def reject_sensitive_files(release_directory: Path) -> None:
    violations = []
    for path in release_directory.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"发行目录含链接或重解析目录：{path}")
        if path.is_file() and is_sensitive_path(path.relative_to(release_directory).as_posix()):
            violations.append(path)
    if violations:
        rendered = ", ".join(str(path.relative_to(release_directory)) for path in violations)
        raise RuntimeError(f"发行目录含运行数据：{rendered}")


def create_zip(release_directory: Path, archive: Path, *, updater_directory: Path = ROOT / "tools") -> None:
    updater_files = {"更新.cmd": updater_directory / "update.cmd", "update.ps1": updater_directory / "update.ps1"}
    for source in updater_files.values():
        if not source.is_file():
            raise FileNotFoundError(f"缺少离线更新器：{source}")
    reject_sensitive_files(release_directory)
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(release_directory.rglob("*")):
            if path.is_file():
                output.write(path, "SparkKeeper/" + path.relative_to(release_directory).as_posix())
        for name, source in updater_files.items():
            output.write(source, name)


def main() -> None:
    if not PYTHON.is_file() or not PLAYWRIGHT.is_file():
        raise RuntimeError("缺少项目虚拟环境，请先安装开发依赖")
    if Path(sys.executable).resolve() != PYTHON.resolve():
        raise RuntimeError("请使用项目 .venv/Scripts/python.exe 构建，以记录实际依赖版本")
    provenance = build_provenance(ROOT)
    version = project_version(ROOT)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    run([str(PLAYWRIGHT), "install", "chromium"], env=env)
    if not LOCAL_BROWSERS.is_dir():
        raise RuntimeError("Playwright 本地 Chromium 未安装")

    release_root = ROOT / "outputs" / "release"
    build_root = ROOT / "work" / "pyinstaller"
    pyinstaller_dist = build_root / "dist"
    command = portable_command(ROOT, PYTHON)
    release_name = f"SparkKeeper-{release_version(version)}-win64"
    release_directory = release_root / "SparkKeeper"
    archive = release_root / f"{release_name}.zip"

    shutil.rmtree(build_root, ignore_errors=True)
    shutil.rmtree(release_directory, ignore_errors=True)
    release_root.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)
    archive.with_suffix(archive.suffix + ".sha256").unlink(missing_ok=True)
    build_root.mkdir(parents=True, exist_ok=True)

    run(command, env=env)

    built_directory = pyinstaller_dist / "SparkKeeper"
    if not (built_directory / "SparkKeeper.exe").is_file():
        raise RuntimeError("PyInstaller 未生成 SparkKeeper.exe")
    bundled_browser_source = (
        built_directory / "_internal" / "playwright" / "driver" / "package" / ".local-browsers"
    )
    if not bundled_browser_source.is_dir():
        raise RuntimeError("PyInstaller 产物缺少 Playwright Chromium")
    bundled_browser_destination = built_directory / "browsers"
    shutil.move(str(bundled_browser_source), bundled_browser_destination)
    shutil.rmtree(bundled_browser_destination / ".links", ignore_errors=True)
    shutil.rmtree(bundled_browser_destination / "__dirlock", ignore_errors=True)
    shutil.move(str(built_directory), release_directory)
    trim_unused_qt_plugins(release_directory)
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.txt", release_directory)
    copy_licenses(release_directory)
    write_user_guide(release_directory / "使用说明.txt", version)
    reject_sensitive_files(release_directory)
    verify_build_unchanged(provenance, root=ROOT)
    write_release_manifest(release_directory, version, provenance=provenance)
    create_zip(release_directory, archive, updater_directory=ROOT / "tools")
    try:
        verify_build_unchanged(provenance, root=ROOT)
    except (OSError, ValueError, RuntimeError):
        archive.unlink(missing_ok=True)
        (release_directory / "release-manifest.json").unlink(missing_ok=True)
        raise

    digest = sha256_file(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n",
        encoding="ascii",
    )
    shutil.rmtree(build_root, ignore_errors=True)
    print(release_directory)
    print(archive)
    print(digest)


if __name__ == "__main__":
    main()
