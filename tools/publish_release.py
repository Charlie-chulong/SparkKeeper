from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import shlex
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_build = runpy.run_path(str(ROOT / "tools" / "build_portable.py"))
project_version = _build["project_version"]
release_version = _build["release_version"]
sha256_file = _build["sha256_file"]
is_sensitive_path = _build["is_sensitive_path"]
_installer = runpy.run_path(str(ROOT / "tools" / "build_installer.py"))
validate_member = _installer["validate_member"]
unique_object = _installer["unique_object"]
validate_manifest = _installer["validate_manifest"]
validate_installer = _installer["validate_installer"]


def checked(command: list[str], *, root: Path) -> str:
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise RuntimeError(f"命令失败：{shlex.join(command)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo 必须显式指定 GitHub OWNER/REPO")
    if repo.split("/")[1] in {".", ".."}:
        raise ValueError("无效的 GitHub 仓库名称")
    return repo


def remote_repo(url: str) -> str:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", url)
    if not match:
        raise ValueError("远端必须是明确的 github.com HTTPS/SSH 仓库地址")
    return validate_repo(match.group(1))


def validate_git(root: Path, repo: str, tag: str, version: str, remote: str = "origin") -> str:
    validate_repo(repo)
    if tag != f"v{release_version(version)}":
        raise ValueError(f"Git tag 必须严格等于 v{release_version(version)}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
        raise ValueError("无效 remote 名称")
    tracked = checked(["git", "ls-files", "-z"], root=root).split("\0")
    violations = [name for name in tracked if name and (
        is_sensitive_path(name)
        or name.replace("\\", "/").casefold().startswith(("work/", "data/"))
    )]
    if violations:
        raise RuntimeError("Git 跟踪了运行数据，拒绝发布：" + ", ".join(violations))
    if checked(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], root=root):
        raise RuntimeError("Git 工作区不干净（包括未跟踪文件），请先人工审查并提交")
    for push in (False, True):
        command = ["git", "remote", "get-url", "--all"]
        if push:
            command.append("--push")
        urls = checked([*command, remote], root=root).splitlines()
        if not urls or any(remote_repo(url).casefold() != repo.casefold() for url in urls):
            raise RuntimeError(f"remote {remote} 与 --repo {repo} 不一致")
    head = checked(["git", "rev-parse", "HEAD"], root=root)
    tagged = checked(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], root=root)
    if tagged != head:
        raise RuntimeError("发行 tag 必须指向当前 HEAD；本工具不创建或推送 tag")
    return head




def validate_archive(archive: Path, version: str, *, root: Path = ROOT) -> Path:
    if archive.name != f"SparkKeeper-{release_version(version)}-win64.zip":
        raise ValueError("ZIP 文件名与版本不一致")
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    if checksum.read_text("ascii").strip() != f"{sha256_file(archive)}  {archive.name}":
        raise RuntimeError("外部 ZIP.sha256 校验失败")
    with zipfile.ZipFile(archive) as bundle:
        members = {}
        folded = set()
        for info in bundle.infolist():
            validate_member(info.filename)
            name = info.filename
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("ZIP 仅允许普通文件，不允许目录项或符号链接")
            if name.casefold() in folded:
                raise ValueError(f"ZIP 含重复/大小写冲突路径：{name}")
            folded.add(name.casefold())
            members[name] = info
            if is_sensitive_path(name):
                raise RuntimeError(f"发行包含运行数据：{name}")
        for name, source in {"更新.cmd": "update.cmd", "update.ps1": "update.ps1"}.items():
            if name not in members or bundle.read(name) != (root / "tools" / source).read_bytes():
                raise RuntimeError(f"离线更新器缺失或与当前提交不一致：{name}")
        manifest_name = "SparkKeeper/release-manifest.json"
        if manifest_name not in members:
            raise RuntimeError("发行包缺少 release-manifest.json")
        files = validate_manifest(bundle.read(manifest_name), version)
        expected = {"更新.cmd", "update.ps1", manifest_name}
        for name, digest in files.items():
            member = "SparkKeeper/" + name
            expected.add(member)
            if member not in members:
                raise RuntimeError(f"发行包缺少文件：{member}")
            with bundle.open(member) as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != digest:
                raise RuntimeError(f"发行文件 SHA256 不符：{member}")
        if set(members) != expected:
            raise RuntimeError("ZIP 含 manifest 之外的额外文件")
    return checksum


def validate_qt_sources(archive: Path, source_directory: Path | None, *, root: Path) -> list[Path]:
    """Bind separately uploaded LGPL sources to the packaged, committed provenance."""
    with zipfile.ZipFile(archive) as bundle:
        provenance = bundle.read("SparkKeeper/licenses/Qt/manifest.json")
    manifest = json.loads(provenance, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict):
        raise TypeError("Qt 来源清单必须是 JSON 对象")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Qt 来源版本无效")
    if provenance != (root / "third_party" / f"qt-{version}" / "manifest.json").read_bytes():
        raise RuntimeError("包内 Qt 来源清单与当前提交不一致")
    source_directory = source_directory or root / "outputs" / "release" / f"qt-sources-{version}"
    if source_directory.is_symlink() or source_directory.is_junction():
        raise ValueError("Qt 源码目录不能是链接")
    sources = manifest.get("source_archives")
    if not isinstance(sources, list) or len(sources) != 2:
        raise ValueError("Qt 来源必须包含 qtbase 和 pyside-setup 两份源码")
    expected_names = {
        f"qtbase-everywhere-src-{version}.tar.xz",
        f"pyside-setup-everywhere-src-{version}.tar.xz",
    }
    expected_sums = {}
    assets = []
    for source in sources:
        if not isinstance(source, dict):
            raise TypeError("Qt 来源条目无效")
        name = source.get("filename")
        if name not in expected_names or name in expected_sums:
            raise ValueError("Qt 源码文件名重复、不安全或与版本不符")
        digest = source.get("expected_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or source.get("sha256") != digest:
            raise ValueError("Qt 源码来源 SHA256 无效")
        path = source_directory / name
        if path.is_symlink() or path.is_junction() or path.resolve().parent != source_directory.resolve():
            raise ValueError("Qt 源码路径越界或为链接")
        if sha256_file(path) != digest:
            raise RuntimeError(f"Qt 源码 SHA256 校验失败：{name}")
        expected_sums[name] = digest
        assets.append(path.resolve())
    checksum = source_directory / "SHA256SUMS.txt"
    if checksum.is_symlink() or checksum.is_junction():
        raise ValueError("Qt 校验清单不能是链接")
    actual_sums = {}
    for line in checksum.read_text("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match[2] in actual_sums:
            raise ValueError("Qt 校验清单含不安全路径或重复条目")
        actual_sums[match[2]] = match[1]
    if actual_sums != expected_sums:
        raise RuntimeError("Qt SHA256SUMS.txt 与来源清单不一致")
    return [*assets, checksum.resolve()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人工 GitHub Release 预检；默认仅 dry-run，不联网、不创建或推送 tag。")
    parser.add_argument("--repo", required=True, help="明确的 GitHub OWNER/REPO")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--tag", help="正式版 vX.Y.Z；候选版 vX.Y.Z-rc.N")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--installer", type=Path, help="Setup.exe；默认与 ZIP 位于同一目录；须保留本地 .exe.build.json 构建凭据")
    parser.add_argument("--qt-sources", type=Path, help="Qt 对应源码目录；默认从包内 Qt 版本确定")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅本地预检（默认）")
    mode.add_argument("--publish", action="store_true", help="人工确认后仅创建 draft Release 并上传")
    args = parser.parse_args(argv)
    try:
        version = project_version(ROOT)
        public_version = release_version(version)
        tag = args.tag or f"v{public_version}"
        repo = validate_repo(args.repo)
        archive = (args.archive or ROOT / "outputs" / "release" / f"SparkKeeper-{public_version}-win64.zip").resolve()
        head = validate_git(ROOT, repo, tag, version, args.remote)
        checksum = validate_archive(archive, version, root=ROOT)
        installer = (args.installer or archive.with_name(f"SparkKeeper-{public_version}-Setup.exe")).resolve()
        with zipfile.ZipFile(archive) as bundle:
            manifest_digest = hashlib.sha256(bundle.read("SparkKeeper/release-manifest.json")).hexdigest()
        installer_checksum = validate_installer(installer, version, manifest_digest, root=ROOT)
        source_assets = validate_qt_sources(archive, args.qt_sources, root=ROOT)
        command = ["gh", "release", "create", tag, str(installer), str(installer_checksum),
                   str(archive), str(checksum), *map(str, source_assets), "--repo", repo,
                   "--verify-tag", "--draft", "--title", f"SparkKeeper {public_version}",
                   "--notes", "人工预发布草稿：请核对安装/升级与便携说明、SHA256、用户数据保留策略后，在 GitHub 手动发布。"]
        if "rc" in version:
            command.append("--prerelease")
        print(f"本地预检通过：{repo} {tag} HEAD={head}\n{shlex.join(command)}")
        if not args.publish:
            print("DRY-RUN：未联网、未发布、未创建或推送 tag。")
            return 0
        confirmation = f"{repo} {tag}"
        if input(f"仅创建草稿。确认目标后输入 {confirmation}：").strip() != confirmation:
            raise RuntimeError("确认不匹配，未发布")
        # Recheck after the human pause, before any network operation.
        if validate_git(ROOT, repo, tag, version, args.remote) != head or project_version(ROOT) != version:
            raise RuntimeError("人工确认期间版本或提交发生变化")
        validate_archive(archive, version, root=ROOT)
        validate_qt_sources(archive, args.qt_sources, root=ROOT)
        with zipfile.ZipFile(archive) as bundle:
            current_manifest_digest = hashlib.sha256(bundle.read("SparkKeeper/release-manifest.json")).hexdigest()
        validate_installer(installer, version, current_manifest_digest, root=ROOT)
        remote_ref = checked(["gh", "api", f"repos/{repo}/commits/refs/tags/{tag}", "--jq", ".sha"], root=ROOT)
        if remote_ref != head:
            raise RuntimeError("GitHub 远端 tag 与本地提交不一致；请人工核对并推送正确 tag")
        print(checked(command, root=ROOT))
        print("仅已创建草稿并上传；仍需在 GitHub 核对并人工点击发布。")
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, KeyError, zipfile.BadZipFile, EOFError) as exc:
        print(f"拒绝发布：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
