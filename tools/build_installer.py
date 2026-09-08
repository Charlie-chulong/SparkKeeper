from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_build = runpy.run_path(str(ROOT / "tools" / "build_portable.py"))
project_version = _build["project_version"]
release_version = _build["release_version"]
sha256_file = _build["sha256_file"]
is_sensitive_path = _build["is_sensitive_path"]
reject_sensitive_files = _build["reject_sensitive_files"]
validate_provenance = _build["validate_provenance"]


def validate_member(name: str) -> None:
    parts = name.split("/")
    if not name or "\\" in name or any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or any(ord(char) < 32 or char in ':*?"<>|' for char in part)
        or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE)
        for part in parts
    ):
        raise ValueError(f"发行包含不安全路径：{name!r}")
    if parts[0].casefold() == ".installer" or (
        len(parts) == 1 and re.fullmatch(r"unins.*\.(?:exe|dat|msg)", name, re.IGNORECASE)
    ):
        raise ValueError(f"发行包含安装器保留路径：{name!r}")


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"manifest 含重复字段：{key}")
        result[key] = value
    return result


def validate_manifest(content: bytes, version: str) -> dict[str, str]:
    manifest = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest.get("format") != 1 or manifest.get("product") != "SparkKeeper" or manifest.get("version") != version:
        raise RuntimeError("manifest 格式、产品或版本不匹配")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("manifest 文件清单为空或无效")
    folded = set()
    for name, digest in files.items():
        validate_member(name)
        if name.casefold() in folded:
            raise ValueError(f"manifest 含大小写冲突路径：{name}")
        folded.add(name.casefold())
        if is_sensitive_path(name):
            raise RuntimeError(f"发行包含运行数据：{name}")
        if name.casefold() == "release-manifest.json" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("manifest 含自身或无效 SHA256")
    required = {"SparkKeeper.exe", "THIRD_PARTY_NOTICES.txt", "使用说明.txt"}
    if not required <= files.keys() or any(not any(name.startswith(prefix) for name in files) for prefix in ("_internal/", "browsers/", "licenses/")):
        raise RuntimeError("发行包缺少程序、运行库、浏览器、许可或说明")
    return files


def validate_payload(payload: Path, version: str) -> Path:
    if not payload.is_dir():
        raise FileNotFoundError(f"缺少便携 payload，请先运行 tools/build_portable.py：{payload}")
    if payload.is_symlink() or payload.is_junction():
        raise ValueError("payload 目录不能是链接")
    reject_sensitive_files(payload)
    manifest_path = payload / "release-manifest.json"
    files = validate_manifest(manifest_path.read_bytes(), version)
    actual = set()
    folded = set()
    for path in payload.rglob("*"):
        name = path.relative_to(payload).as_posix()
        validate_member(name)
        if is_sensitive_path(name):
            raise RuntimeError(f"发行包含运行数据：{name}")
        if name.casefold() in folded:
            raise ValueError(f"payload 含大小写冲突路径：{name}")
        folded.add(name.casefold())
        if path.is_file():
            actual.add(name)
        elif not path.is_dir():
            raise ValueError(f"payload 含非普通文件：{name}")
    if actual != set(files) | {"release-manifest.json"}:
        raise RuntimeError("payload 缺少文件或含 manifest 之外的额外文件")
    for name, digest in files.items():
        if sha256_file(payload / name) != digest:
            raise RuntimeError(f"payload 文件 SHA256 不符：{name}")
    return manifest_path


def find_iscc(explicit: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"--iscc 指定的编译器不存在：{explicit}")
        return explicit.resolve()
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        value = os.environ.get(variable)
        if value:
            base = Path(value)
            if variable == "LOCALAPPDATA":
                base /= "Programs"
            candidates.append(base / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("未找到 Inno Setup 6 ISCC.exe；请安装 Inno Setup 6，或使用 --iscc 指定编译器完整路径")


def read_installer_metadata(installer: Path) -> dict[str, str]:
    """Read resources as data, never start the installer or execute its code."""
    if sys.platform != "win32":
        raise RuntimeError("安装器 PE 权限/版本资源验证需要 Windows；不能跳过验证发布")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = wintypes.HANDLE
    kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.LoadResource.restype = wintypes.HANDLE
    kernel.LockResource.argtypes = [wintypes.HANDLE]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FreeLibrary.restype = wintypes.BOOL
    module = kernel.LoadLibraryExW(str(installer.resolve()), None, 0x02 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        resource = kernel.FindResourceW(module, 1, 24)
        if not resource:
            raise RuntimeError("安装器缺少 PE manifest")
        size = kernel.SizeofResource(module, resource)
        loaded = kernel.LoadResource(module, resource)
        address = kernel.LockResource(loaded) if loaded else None
        if not address or not size:
            raise RuntimeError("无法读取安装器 PE manifest")
        try:
            tree = ET.fromstring(ctypes.string_at(address, size))
        except ET.ParseError as exc:
            raise RuntimeError("安装器 PE manifest 不是有效 XML") from exc
        levels = [node.attrib for node in tree.iter() if node.tag.rsplit("}", 1)[-1] == "requestedExecutionLevel"]
        if len(levels) != 1 or levels[0].get("level") != "asInvoker" or levels[0].get("uiAccess", "false") != "false":
            raise RuntimeError("安装器必须声明 asInvoker 且不能请求 uiAccess")
    finally:
        kernel.FreeLibrary(module)
    return {"execution_level": "asInvoker", **read_pe_version(installer)}


def read_pe_version(installer: Path) -> dict[str, str]:
    if sys.platform != "win32":
        raise RuntimeError("PE 编译器版本资源读取需要 Windows")
    from ctypes import wintypes


    version_api = ctypes.WinDLL("version", use_last_error=True)
    version_api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version_api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version_api.GetFileVersionInfoW.restype = wintypes.BOOL
    version_api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version_api.VerQueryValueW.restype = wintypes.BOOL
    size = version_api.GetFileVersionInfoSizeW(str(installer), None)
    if not size:
        raise RuntimeError("安装器缺少版本资源")
    data = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(installer), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())

    def query(name: str) -> tuple[ctypes.c_void_p, int]:
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_api.VerQueryValueW(data, name, ctypes.byref(pointer), ctypes.byref(length)):
            raise RuntimeError(f"安装器缺少版本资源字段：{name}")
        return pointer, length.value

    pointer, length = query(r"\VarFileInfo\Translation")
    if length < 4:
        raise RuntimeError("安装器版本资源语言表无效")
    translation = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
    prefix = f"\\StringFileInfo\\{translation[0]:04x}{translation[1]:04x}\\"
    result = {}
    for name in ("ProductName", "ProductVersion", "FileDescription"):
        pointer, length = query(prefix + name)
        result[name] = ctypes.wstring_at(pointer, length).rstrip(" \0")
    return result


def validate_installer_metadata(installer: Path, version: str) -> dict[str, str]:
    metadata = read_installer_metadata(installer)
    expected = {"execution_level": "asInvoker", "ProductName": "SparkKeeper", "ProductVersion": version, "FileDescription": "SparkKeeper Setup"}
    if metadata != expected:
        raise RuntimeError(f"安装器权限、产品或版本资源不匹配：{metadata}")
    return metadata


def receipt_path(installer: Path) -> Path:
    return installer.with_suffix(".exe.build.json")


def installer_inputs(root: Path) -> dict[str, str]:
    return {name: sha256_file(root / "tools" / name)
            for name in ("build_installer.py", "installer.iss", "installer_guard.ps1", "update.ps1",
                         "installer/ChineseSimplified.isl", "installer/LICENSE.txt", "maintenance_tasks.ps1")}


def compiler_identity(compiler: Path) -> dict[str, str]:
    return {"sha256": sha256_file(compiler), "version": read_pe_version(compiler)["ProductVersion"]}


def validate_installer(installer: Path, version: str, manifest_content: bytes, *, root: Path = ROOT) -> Path:
    if installer.name != f"SparkKeeper-{release_version(version)}-Setup.exe":
        raise ValueError("安装器文件名与版本不一致")
    checksum = installer.with_suffix(".exe.sha256")
    digest = sha256_file(installer)
    if checksum.read_text("ascii").strip() != f"{digest}  {installer.name}":
        raise RuntimeError("安装器 EXE.sha256 校验失败")
    receipt = json.loads(receipt_path(installer).read_bytes(), object_pairs_hook=unique_object)
    if not isinstance(receipt, dict):
        raise TypeError("安装器构建凭据必须是对象")
    validate_manifest(manifest_content, version)
    manifest = json.loads(manifest_content, object_pairs_hook=unique_object)
    provenance = validate_provenance(manifest.get("build_provenance"), root=root)
    compiler = receipt.get("compiler")
    if (not isinstance(compiler, dict) or set(compiler) != {"sha256", "version"}
            or not isinstance(compiler.get("version"), str) or not compiler["version"]
            or not isinstance(compiler.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", compiler["sha256"])):
        raise RuntimeError("安装器构建凭据缺少编译器版本或摘要")
    expected = {"format": 2, "version": version, "installer_sha256": digest,
                "inputs_sha256": installer_inputs(root),
                "payload_manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
                "payload_build_provenance": provenance, "compiler": compiler}
    if receipt != expected:
        raise RuntimeError("安装器构建凭据与 EXE、当前安装脚本或 ZIP payload 不一致；请重新构建安装器")
    validate_installer_metadata(installer, version)
    return checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="由已验证的便携 payload 构建当前用户 Inno Setup 安装器（不会运行安装器）")
    parser.add_argument("--iscc", type=Path, help="Inno Setup 6 ISCC.exe 完整路径")
    parser.add_argument("--payload", type=Path, help="默认 outputs/release/SparkKeeper")
    parser.add_argument("--output-dir", type=Path, help="默认 outputs/release")
    args = parser.parse_args(argv)
    try:
        version = project_version(ROOT)
        payload = (args.payload or ROOT / "outputs" / "release" / "SparkKeeper").resolve()
        output = (args.output_dir or ROOT / "outputs" / "release").resolve()
        manifest = validate_payload(payload, version)
        manifest_digest = sha256_file(manifest)
        provenance = validate_provenance(
            json.loads(manifest.read_bytes(), object_pairs_hook=unique_object).get("build_provenance"),
            root=ROOT,
        )
        compiler = find_iscc(args.iscc)
        compiler_info = compiler_identity(compiler)
        script = ROOT / "tools" / "installer.iss"
        inputs = installer_inputs(ROOT)
        base = f"SparkKeeper-{release_version(version)}-Setup"
        if output == payload or output.is_relative_to(payload):
            raise ValueError("安装器输出目录不能位于 payload 内")
        output.mkdir(parents=True, exist_ok=True)
        # A failed compiler must never leave a new checksum blessing an older EXE.
        with tempfile.TemporaryDirectory(prefix="sparkkeeper-setup-", dir=output) as temporary:
            staging = Path(temporary)
            command = [str(compiler), f"/DAppVersion={version}", f"/DPayloadDir={payload}",
                       f"/DOutputDir={staging}", f"/DOutputBaseFilename={base}", str(script)]
            result = subprocess.run(command, cwd=ROOT, check=False)
            if result.returncode:
                raise RuntimeError(f"ISCC 编译失败，退出码 {result.returncode}")
            installer = staging / f"{base}.exe"
            if not installer.is_file():
                raise RuntimeError("ISCC 未生成预期的 Setup.exe")
            validate_payload(payload, version)
            validate_provenance(provenance, root=ROOT)
            if (sha256_file(manifest) != manifest_digest or installer_inputs(ROOT) != inputs
                    or project_version(ROOT) != version or compiler_identity(compiler) != compiler_info):
                raise RuntimeError("编译期间版本、payload 或安装脚本发生变化")
            validate_installer_metadata(installer, version)
            digest = sha256_file(installer)
            checksum = installer.with_suffix(".exe.sha256")
            checksum.write_text(f"{digest}  {installer.name}\n", encoding="ascii")
            receipt = receipt_path(installer)
            receipt.write_text(json.dumps({
                "format": 2, "version": version, "installer_sha256": digest,
                "inputs_sha256": inputs, "payload_manifest_sha256": manifest_digest,
                "payload_build_provenance": provenance, "compiler": compiler_info,
            }, indent=2) + "\n", encoding="utf-8")
            for artifact in (installer, checksum, receipt):
                artifact.replace(output / artifact.name)
        print(output / f"{base}.exe")
        print(digest)
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, ET.ParseError) as exc:
        print(f"拒绝构建安装器：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
