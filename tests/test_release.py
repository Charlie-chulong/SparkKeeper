from __future__ import annotations

import ctypes
import hashlib
import json
import runpy
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
build = runpy.run_path(str(ROOT / "tools" / "build_portable.py"))
publish = runpy.run_path(str(ROOT / "tools" / "publish_release.py"))
installer_build = runpy.run_path(str(ROOT / "tools" / "build_installer.py"))


def setup_metadata(version="3.0.0"):
    return {"execution_level": "asInvoker", "ProductName": "SparkKeeper",
            "ProductVersion": version, "FileDescription": "SparkKeeper Setup"}


def setup_artifacts(root, program, version="3.0.0"):
    installer = root / f"SparkKeeper-{build['release_version'](version)}-Setup.exe"
    installer.write_bytes(b"isolated compiler output fixture")
    digest = build["sha256_file"](installer)
    installer.with_suffix(".exe.sha256").write_text(f"{digest}  {installer.name}\n", "ascii")
    installer_build["receipt_path"](installer).write_text(json.dumps({
        "format": 2, "version": version, "installer_sha256": digest,
        "inputs_sha256": installer_build["installer_inputs"](root),
        "payload_manifest_sha256": build["sha256_file"](program / "release-manifest.json"),
        "payload_build_provenance": json.loads((program / "release-manifest.json").read_bytes())["build_provenance"],
        "compiler": {"version": "6.5.0", "sha256": "a" * 64},
    }), "utf-8")
    return installer


@pytest.fixture
def release_bundle(tmp_path, monkeypatch):
    program = tmp_path / "arbitrary-version-directory"
    payload = {
        "SparkKeeper.exe": b"isolated packaging fixture",
        "_internal/runtime.dll": b"runtime",
        "browsers/chrome.exe": b"browser",
        "licenses/NOTICE.txt": b"license",
        "THIRD_PARTY_NOTICES.txt": b"notices",
        "使用说明.txt": b"guide",
    }
    for name, data in payload.items():
        path = program / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("build_portable.py", "portable_entry.py", "build_installer.py"):
        (tools / name).write_text("isolated build source fixture", "utf-8")
    for name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.txt"):
        (tmp_path / name).write_text("isolated project input", "utf-8")
    package = tmp_path / "src" / "spark_keeper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "3.0.0"\n', "utf-8")
    (package / "app.py").write_text('MESSAGE = "source A"\n', "utf-8")
    (package / "assets").mkdir()
    (package / "assets" / "app-icon.ico").write_bytes(b"icon fixture")
    toolchain = {"PyInstaller": "6.15.0", "playwright": "1.50.0",
                 "PySide6-Essentials": "6.11.2", "windows-toasts": "1.3.0"}
    monkeypatch.setitem(build["build_provenance"].__globals__, "distribution",
                        lambda name: SimpleNamespace(version=toolchain[name]))
    monkeypatch.setitem(build["build_provenance"].__globals__, "distributions",
                        lambda: [SimpleNamespace(metadata={"Name": name}, version=version)
                                 for name, version in toolchain.items()])
    monkeypatch.setitem(installer_build["main"].__globals__, "compiler_identity",
                        lambda path: {"version": "6.5.0", "sha256": "a" * 64})
    (tools / "update.cmd").write_bytes(b"@echo off\r\n")
    (tools / "update.ps1").write_bytes(b"Write-Output fixture\r\n")
    for name in ("installer.iss", "installer_guard.ps1", "maintenance_tasks.ps1"):
        (tools / name).write_text("isolated installer source fixture", "utf-8")
    (tools / "installer").mkdir()
    (tools / "installer" / "ChineseSimplified.isl").write_text("isolated language fixture", "utf-8")
    (tools / "installer" / "LICENSE.txt").write_text("isolated language license fixture", "utf-8")
    sources = tmp_path / "outputs" / "release" / "qt-sources-6.11.2"
    sources.mkdir(parents=True)
    entries = []
    sums = []
    for name in ("qtbase-everywhere-src-6.11.2.tar.xz", "pyside-setup-everywhere-src-6.11.2.tar.xz"):
        content = name.encode()
        digest = hashlib.sha256(content).hexdigest()
        (sources / name).write_bytes(content)
        entries.append({"filename": name, "expected_sha256": digest, "sha256": digest})
        sums.append(f"{digest}  {name}\n")
    (sources / "SHA256SUMS.txt").write_text("".join(sums), "ascii")
    provenance = json.dumps({"version": "6.11.2", "source_archives": entries}).encode()
    for path in (program / "licenses" / "Qt" / "manifest.json", tmp_path / "third_party" / "qt-6.11.2" / "manifest.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(provenance)
    build["write_release_manifest"](program, "3.0.0", provenance=build["build_provenance"](tmp_path))
    archive = tmp_path / "SparkKeeper-3.0.0-win64.zip"
    build["create_zip"](program, archive, updater_directory=tools)
    checksum(archive)
    setup_artifacts(tmp_path, program)
    for globals_ in (installer_build["validate_installer_metadata"].__globals__,
                     publish["validate_installer"].__globals__):
        monkeypatch.setitem(globals_, "read_installer_metadata", lambda path: setup_metadata())
    return tmp_path, program, archive


def checksum(archive):
    archive.with_suffix(".zip.sha256").write_text(
        f"{build['sha256_file'](archive)}  {archive.name}\n", encoding="ascii"
    )


def rewrite(archive, *, changed=None, extra=None, removed=None):
    with zipfile.ZipFile(archive) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    files.update(changed or {})
    files.update(extra or {})
    for name in removed or ():
        files.pop(name)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    checksum(archive)


def test_fixed_layout_manifest_and_outer_checksum(release_bundle):
    root, program, archive = release_bundle
    manifest = json.loads((program / "release-manifest.json").read_text("utf-8"))
    assert manifest["format"] == 1
    assert manifest["product"] == "SparkKeeper"
    assert manifest["version"] == "3.0.0"
    assert "release-manifest.json" not in manifest["files"]
    for name, digest in manifest["files"].items():
        assert digest == hashlib.sha256((program / name).read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        assert {name.split("/")[0] for name in bundle.namelist()} == {"SparkKeeper", "更新.cmd", "update.ps1"}
    assert publish["validate_archive"](archive, "3.0.0", root=root) == archive.with_suffix(".zip.sha256")


@pytest.mark.parametrize("version", ["3.0.0", "3.0.0rc1", "3.1.2rc10"])
def test_release_versions(version):
    assert build["validate_version"](version) == version


@pytest.mark.parametrize("version", ["v3.0.0", "03.0.0", "3.0", "3.0.0.dev18", "3.0.0+local", "3.0.0RC1", "3.0.0rc0", "3.0.0rc01", "3.0.0-rc.1", "3.0.0post1"])
def test_noncanonical_or_unreleased_versions_rejected(version):
    with pytest.raises(ValueError):
        build["validate_version"](version)


def test_version_is_read_without_importing_business_code(tmp_path):
    source = tmp_path / "src" / "spark_keeper" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('raise RuntimeError("must not execute")\n__version__ = "3.0.0rc2"\n', "utf-8")
    assert build["project_version"](tmp_path) == "3.0.0rc2"
    source.write_text('__version__ = "3.0.0"\n__version__ = "3.0.1"\n', "utf-8")
    with pytest.raises(ValueError, match="唯一"):
        build["project_version"](tmp_path)


def test_corrupt_zip_rejected_before_opening(release_bundle):
    root, _, archive = release_bundle
    with archive.open("ab") as handle:
        handle.write(b"corruption")
    with pytest.raises(RuntimeError, match="ZIP.sha256"):
        publish["validate_archive"](archive, "3.0.0", root=root)


@pytest.mark.parametrize("change", ["tampered", "missing", "extra", "updater", "traversal", "sensitive", "product"])
def test_bad_release_refused_even_with_fresh_outer_hash(release_bundle, change):
    root, _, archive = release_bundle
    if change == "tampered":
        rewrite(archive, changed={"SparkKeeper/SparkKeeper.exe": b"modified"})
    elif change == "missing":
        rewrite(archive, removed=["SparkKeeper/browsers/chrome.exe"])
    elif change == "extra":
        rewrite(archive, extra={"SparkKeeper/stale.dll": b"old"})
    elif change == "updater":
        rewrite(archive, changed={"update.ps1": b"modified updater"})
    elif change == "traversal":
        rewrite(archive, extra={"SparkKeeper/../escape": b"bad"})
    elif change == "sensitive":
        rewrite(archive, extra={"SparkKeeper/auth-state.bin": b"private"})
    else:
        with zipfile.ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read("SparkKeeper/release-manifest.json"))
        manifest["product"] = "OtherProduct"
        rewrite(archive, changed={"SparkKeeper/release-manifest.json": json.dumps(manifest).encode()})
    with pytest.raises((ValueError, RuntimeError)):
        publish["validate_archive"](archive, "3.0.0", root=root)


@pytest.mark.parametrize("url", ["https://github.com/owner/repo.git", "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git"])
def test_remote_identity(url):
    assert publish["remote_repo"](url) == "owner/repo"


@pytest.mark.parametrize("url", ["https://github.com.evil/owner/repo", "https://evil/owner/repo", "file:///repo", "https://github.com/owner/repo/extra"])
def test_foreign_remote_rejected(url):
    with pytest.raises(ValueError):
        publish["remote_repo"](url)


def fake_git(monkeypatch, *, dirty="", tracked="src/app.py\0", remote="https://github.com/owner/repo.git", tagged="abc"):
    calls = []

    def checked(command, *, root):
        calls.append(command)
        assert command[0] == "git"
        if command[1] == "ls-files":
            return tracked
        if command[1] == "status":
            return dirty
        if command[1] == "remote":
            return remote
        return "abc" if command[-1] == "HEAD" else tagged

    monkeypatch.setitem(publish["validate_git"].__globals__, "checked", checked)
    return calls


def test_git_preflight_is_read_only_and_checks_head(monkeypatch, tmp_path):
    calls = fake_git(monkeypatch)
    assert publish["validate_git"](tmp_path, "owner/repo", "v3.0.0", "3.0.0") == "abc"
    assert {call[1] for call in calls} == {"ls-files", "status", "remote", "rev-parse"}


@pytest.mark.parametrize("options", [
    {"dirty": "?? new.txt"}, {"tracked": "outputs/private.log\0"},
    {"tracked": "work/spark-keeper-task.xml\0"},
    {"remote": "https://github.com/other/repo.git"}, {"tagged": "different"},
])
def test_git_preflight_refuses_unsafe_state(monkeypatch, tmp_path, options):
    fake_git(monkeypatch, **options)
    with pytest.raises(RuntimeError):
        publish["validate_git"](tmp_path, "owner/repo", "v3.0.0", "3.0.0")


def test_tag_must_match_version(monkeypatch, tmp_path):
    calls = fake_git(monkeypatch)
    with pytest.raises(ValueError, match="v3.0.0"):
        publish["validate_git"](tmp_path, "owner/repo", "3.0.0", "3.0.0")
    assert not calls


def test_default_dry_run_never_calls_gh_or_prompts(monkeypatch, release_bundle, capsys):
    root, _, archive = release_bundle
    globals_ = publish["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "project_version", lambda root: "3.0.0")
    monkeypatch.setitem(globals_, "validate_git", lambda *args: "abc")
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("dry-run must not prompt"))
    monkeypatch.setitem(globals_, "checked", lambda *args, **kwargs: pytest.fail("dry-run must not execute gh"))
    assert publish["main"](["--repo", "owner/repo", "--archive", str(archive)]) == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_publish_requires_confirmation_and_only_creates_draft(monkeypatch, release_bundle):
    root, _, archive = release_bundle
    globals_ = publish["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "project_version", lambda root: "3.0.0")
    monkeypatch.setitem(globals_, "validate_git", lambda *args: "abc")
    calls = []
    def checked(command, *, root):
        calls.append(command)
        return "abc" if command[1] == "api" else "draft-url"
    monkeypatch.setitem(globals_, "checked", checked)
    args = ["--repo", "owner/repo", "--archive", str(archive), "--publish"]
    monkeypatch.setattr("builtins.input", lambda *args: "wrong")
    assert publish["main"](args) == 1
    assert not calls
    monkeypatch.setattr("builtins.input", lambda *args: "owner/repo v3.0.0")
    assert publish["main"](args) == 0
    assert calls[0][:2] == ["gh", "api"]
    assert calls[1][:3] == ["gh", "release", "create"]
    assert "--draft" in calls[1] and "--verify-tag" in calls[1]
    expected_assets = {
        archive.name, archive.with_suffix(".zip.sha256").name,
        "SparkKeeper-3.0.0-Setup.exe", "SparkKeeper-3.0.0-Setup.exe.sha256",
        "qtbase-everywhere-src-6.11.2.tar.xz", "pyside-setup-everywhere-src-6.11.2.tar.xz",
        "SHA256SUMS.txt",
    }
    asset_args = calls[1][4:calls[1].index("--repo")]
    assert {Path(argument).name for argument in asset_args} == expected_assets


def test_qt_sources_are_verified_against_packaged_provenance(release_bundle):
    root, _, archive = release_bundle
    assets = publish["validate_qt_sources"](archive, None, root=root)
    assert len(assets) == 3
    assert assets[-1].name == "SHA256SUMS.txt"
    assert {path.suffix for path in assets[:2]} == {".xz"}
    assets[0].write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="SHA256"):
        publish["validate_qt_sources"](archive, None, root=root)


def test_qt_source_checksum_rejects_path_traversal(release_bundle):
    root, _, archive = release_bundle
    checksum = root / "outputs" / "release" / "qt-sources-6.11.2" / "SHA256SUMS.txt"
    checksum.write_text("0" * 64 + "  ../outside.tar.xz\n", "ascii")
    with pytest.raises(ValueError, match="不安全路径"):
        publish["validate_qt_sources"](archive, None, root=root)


@pytest.mark.parametrize("python_version,public_version", [
    ("3.0.0", "3.0.0"), ("3.0.1rc1", "3.0.1-rc.1"), ("3.0.1rc12", "3.0.1-rc.12"),
])
def test_public_release_version_is_derived(python_version, public_version):
    assert build["release_version"](python_version) == public_version


def test_rc_archive_and_tag_derive_from_pep440_manifest(release_bundle, monkeypatch):
    root, program, final_archive = release_bundle
    version = "3.0.1rc1"
    (root / "src" / "spark_keeper" / "__init__.py").write_text(f'__version__ = "{version}"\n', "utf-8")
    build["write_release_manifest"](program, version, provenance=build["build_provenance"](root))
    archive = final_archive.with_name("SparkKeeper-3.0.1-rc.1-win64.zip")
    build["create_zip"](program, archive, updater_directory=root / "tools")
    checksum(archive)
    assert json.loads((program / "release-manifest.json").read_text("utf-8"))["version"] == version
    publish["validate_archive"](archive, version, root=root)
    fake_git(monkeypatch)
    assert publish["validate_git"](root, "owner/repo", "v3.0.1-rc.1", version) == "abc"
    with pytest.raises(ValueError, match="v3.0.1-rc.1"):
        publish["validate_git"](root, "owner/repo", "v3.0.1rc1", version)


def test_payload_requires_existing_complete_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="payload"):
        installer_build["validate_payload"](tmp_path / "missing", "3.0.0")


@pytest.mark.parametrize("change", ["version", "hash", "extra", "missing", "sensitive", "manifest", "case", "incomplete"])
def test_installer_rejects_invalid_payload(release_bundle, change):
    _, program, _ = release_bundle
    version = "3.0.0"
    if change == "version":
        version = "3.0.1"
    elif change == "hash":
        (program / "SparkKeeper.exe").write_bytes(b"tampered")
    elif change == "extra":
        (program / "stale.dll").write_bytes(b"extra")
    elif change == "missing":
        (program / "SparkKeeper.exe").unlink()
    elif change == "sensitive":
        (program / "auth-state.bin").write_bytes(b"secret")
    elif change == "manifest":
        (program / "release-manifest.json").unlink()
    else:
        manifest = json.loads((program / "release-manifest.json").read_bytes())
        if change == "case":
            manifest["files"]["sparkkeeper.exe"] = manifest["files"]["SparkKeeper.exe"]
        else:
            del manifest["files"]["SparkKeeper.exe"]
            (program / "SparkKeeper.exe").unlink()
        (program / "release-manifest.json").write_text(json.dumps(manifest), "utf-8")
    with pytest.raises((OSError, ValueError, RuntimeError)):
        installer_build["validate_payload"](program, version)


def test_installer_accepts_verified_payload(release_bundle):
    _, program, _ = release_bundle
    assert installer_build["validate_payload"](program, "3.0.0") == program / "release-manifest.json"


def test_missing_compiler_is_actionable(tmp_path, monkeypatch):
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))
    with pytest.raises(FileNotFoundError, match="--iscc"):
        installer_build["find_iscc"]()
    with pytest.raises(FileNotFoundError, match="--iscc"):
        installer_build["find_iscc"](tmp_path / "missing.exe")


def test_compiler_standard_user_location_and_explicit_override(tmp_path, monkeypatch):
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))
    compiler = tmp_path / "LOCALAPPDATA" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"compiler fixture")
    assert installer_build["find_iscc"]() == compiler.resolve()
    other = tmp_path / "custom-iscc.exe"
    other.write_bytes(b"compiler fixture")
    assert installer_build["find_iscc"](other) == other.resolve()


@pytest.mark.parametrize("change", ["missing", "hash", "receipt", "payload", "script", "guard", "updater", "language", "language_license", "maintenance_helper", "version", "privileges", "product"])
def test_publish_rejects_invalid_installer(release_bundle, monkeypatch, change):
    root, program, _ = release_bundle
    installer = root / "SparkKeeper-3.0.0-Setup.exe"
    manifest_content = (program / "release-manifest.json").read_bytes()
    if change == "missing":
        installer.unlink()
    elif change == "hash":
        installer.write_bytes(b"tampered")
    elif change == "receipt":
        installer_build["receipt_path"](installer).unlink()
    elif change == "payload":
        manifest_content = manifest_content.replace(b"runtime.dll", b"other.dll")
    elif change in {"script", "guard", "updater", "language", "language_license", "maintenance_helper"}:
        name = {"script": "installer.iss", "guard": "installer_guard.ps1", "updater": "update.ps1",
                "language": "installer/ChineseSimplified.isl", "language_license": "installer/LICENSE.txt",
                "maintenance_helper": "maintenance_tasks.ps1"}[change]
        (root / "tools" / name).write_bytes(b"changed source")
    else:
        metadata = setup_metadata()
        metadata[{"version": "ProductVersion", "privileges": "execution_level", "product": "ProductName"}[change]] = "wrong"
        monkeypatch.setitem(publish["validate_installer"].__globals__, "read_installer_metadata", lambda path: metadata)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        publish["validate_installer"](installer, "3.0.0", manifest_content, root=root)


def test_rc_installer_name_and_resources_match_pep440(release_bundle, monkeypatch):
    root, program, _ = release_bundle
    version = "3.0.1rc2"
    (root / "src" / "spark_keeper" / "__init__.py").write_text(f'__version__ = "{version}"\n', "utf-8")
    build["write_release_manifest"](program, version, provenance=build["build_provenance"](root))
    installer = setup_artifacts(root, program, version)
    assert installer.name == "SparkKeeper-3.0.1-rc.2-Setup.exe"
    monkeypatch.setitem(publish["validate_installer"].__globals__, "read_installer_metadata", lambda path: setup_metadata(version))
    checksum_path = publish["validate_installer"](installer, version, (program / "release-manifest.json").read_bytes(), root=root)
    assert checksum_path.name == installer.name + ".sha256"
    with pytest.raises(ValueError, match="文件名"):
        publish["validate_installer"](installer, "3.0.1", b"", root=root)


def test_build_stages_output_and_generates_bound_checksum(release_bundle, monkeypatch):
    root, program, _ = release_bundle
    globals_ = installer_build["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "project_version", lambda root: "3.0.0")
    monkeypatch.setitem(globals_, "find_iscc", lambda explicit: root / "ISCC.exe")
    calls = []

    def compile_(command, **kwargs):
        calls.append(command)
        defines = dict(argument[2:].split("=", 1) for argument in command if argument.startswith("/D"))
        assert defines["AppVersion"] == "3.0.0"
        assert defines["PayloadDir"] == str(program.resolve())
        assert "TestAppId" not in defines
        (Path(defines["OutputDir"]) / (defines["OutputBaseFilename"] + ".exe")).write_bytes(b"compiled fixture")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(globals_["subprocess"], "run", compile_)
    output = root / "setup-output"
    assert installer_build["main"](["--payload", str(program), "--output-dir", str(output)]) == 0
    assert len(calls) == 1
    installer = output / "SparkKeeper-3.0.0-Setup.exe"
    installer_build["validate_installer"](installer, "3.0.0", (program / "release-manifest.json").read_bytes(), root=root)
    assert {path.name for path in output.iterdir()} == {
        installer.name, installer.name + ".sha256", installer.name + ".build.json",
    }


def test_failed_compile_does_not_bless_stale_output(release_bundle, monkeypatch):
    root, program, _ = release_bundle
    installer = root / "SparkKeeper-3.0.0-Setup.exe"
    original = installer.read_bytes()
    original_sum = installer.with_suffix(".exe.sha256").read_bytes()
    globals_ = installer_build["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "project_version", lambda root: "3.0.0")
    monkeypatch.setitem(globals_, "find_iscc", lambda explicit: root / "ISCC.exe")
    monkeypatch.setattr(globals_["subprocess"], "run", lambda *args, **kwargs: type("Completed", (), {"returncode": 2})())
    assert installer_build["main"](["--payload", str(program), "--output-dir", str(root)]) == 1
    assert installer.read_bytes() == original
    assert installer.with_suffix(".exe.sha256").read_bytes() == original_sum


def test_default_preflight_requires_installer_before_network(release_bundle, monkeypatch):
    root, _, archive = release_bundle
    (root / "SparkKeeper-3.0.0-Setup.exe").unlink()
    globals_ = publish["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "project_version", lambda root: "3.0.0")
    monkeypatch.setitem(globals_, "validate_git", lambda *args: "abc")
    monkeypatch.setitem(globals_, "checked", lambda *args, **kwargs: pytest.fail("preflight must remain offline"))
    assert publish["main"](["--repo", "owner/repo", "--archive", str(archive)]) == 1


@pytest.mark.parametrize("name", [".installer/installer_guard.ps1", ".INSTALLER/update.ps1",
                                  "unins000.exe", "UNINS001.DAT", "unins-custom.msg"])
def test_installer_payload_cannot_overwrite_installer_management_files(release_bundle, name):
    root, program, _ = release_bundle
    path = program / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unexpected installer management payload")
    build["write_release_manifest"](program, "3.0.0", provenance=build["build_provenance"](root))
    with pytest.raises(ValueError, match="保留路径"):
        installer_build["validate_payload"](program, "3.0.0")


@pytest.mark.parametrize("product", ["SparkKeeper", " SparkKeeper", "SparkKeeper Other"])
def test_pe_resource_padding_is_trimmed_without_weakening_identity(tmp_path, monkeypatch, product):
    from ctypes import wintypes

    # Match the actual Inno resource padding observed in compiled Setup.exe.
    manifest = ctypes.create_string_buffer(
        b'<assembly><requestedExecutionLevel level="asInvoker" uiAccess="false"/></assembly>'
    )
    translation = (ctypes.c_ushort * 2)(0x0409, 0x04B0)
    values = {"ProductName": product + " " * 49, "ProductVersion": "3.0.2" + " " * 45,
              "FileDescription": "SparkKeeper Setup" + " " * 43}
    buffers = {name: ctypes.create_unicode_buffer(value) for name, value in values.items()}
    kernel = SimpleNamespace(
        LoadLibraryExW=Mock(return_value=1), FindResourceW=Mock(return_value=1),
        LoadResource=Mock(return_value=1), LockResource=Mock(return_value=ctypes.addressof(manifest)),
        SizeofResource=Mock(return_value=len(manifest.value)), FreeLibrary=Mock(return_value=True),
    )

    def query(_data, name, pointer, length):
        if name == r"\VarFileInfo\Translation":
            buffer, size = translation, ctypes.sizeof(translation)
        else:
            buffer = buffers[name.rsplit("\\", 1)[-1]]
            size = len(buffer)
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        ctypes.cast(length, ctypes.POINTER(wintypes.UINT))[0] = size
        return True

    version_api = SimpleNamespace(
        GetFileVersionInfoSizeW=Mock(return_value=16), GetFileVersionInfoW=Mock(return_value=True),
        VerQueryValueW=Mock(side_effect=query),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else version_api, raising=False)
    monkeypatch.setitem(installer_build["read_installer_metadata"].__globals__, "sys", SimpleNamespace(platform="win32"))
    executable = tmp_path / "fixture.exe"
    if product == "SparkKeeper":
        assert installer_build["validate_installer_metadata"](executable, "3.0.2") == setup_metadata("3.0.2")
    else:
        with pytest.raises(RuntimeError, match="不匹配"):
            installer_build["validate_installer_metadata"](executable, "3.0.2")
    kernel.FreeLibrary.assert_called_once_with(1)


@pytest.mark.parametrize("name", [
    "src/spark_keeper/app.py", "src/spark_keeper/assets/app-icon.ico",
    "src/spark_keeper/new_module.py", "tools/build_portable.py", "tools/portable_entry.py",
    "pyproject.toml", "third_party/qt-6.11.2/manifest.json",
])
def test_source_change_rejects_self_consistent_same_version_assets(release_bundle, name):
    root, program, archive = release_bundle
    publish["validate_archive"](archive, "3.0.0", root=root)
    manifest_before = (program / "release-manifest.json").read_bytes()
    archive_before = archive.read_bytes()
    (root / name).write_bytes(b"source B, same project version")
    with pytest.raises(RuntimeError, match="构建来源"):
        publish["validate_archive"](archive, "3.0.0", root=root)
    assert archive.read_bytes() == archive_before
    assert (program / "release-manifest.json").read_bytes() == manifest_before


def test_new_head_cannot_publish_old_business_source(release_bundle, monkeypatch, capsys):
    root, _, archive = release_bundle
    (root / "src" / "spark_keeper" / "app.py").write_text('MESSAGE = "source B"\n')
    globals_ = publish["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "validate_git", lambda *args: "new-head")
    network = Mock(side_effect=AssertionError("preflight must not invoke gh"))
    monkeypatch.setitem(globals_, "checked", network)
    assert publish["main"](["--repo", "owner/repo", "--archive", str(archive)]) == 1
    assert "构建来源" in capsys.readouterr().err
    network.assert_not_called()


def test_documentation_caches_and_runtime_outputs_are_not_source_inputs(release_bundle):
    root, program, archive = release_bundle
    for name in ("README.md", "CHANGELOG.md", "docs/release.txt", "tests/test_example.py",
                 "work/local-data/private.db", "outputs/unused.bin",
                 "src/spark_keeper/__pycache__/app.cpython-312.pyc",
                 "src/spark_keeper/notes.md"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unrelated change in synthetic checkout")
    publish["validate_archive"](archive, "3.0.0", root=root)
    publish["validate_installer"](
        root / "SparkKeeper-3.0.0-Setup.exe", "3.0.0",
        (program / "release-manifest.json").read_bytes(), root=root,
    )


def test_removed_business_module_is_a_source_change(release_bundle):
    root, _, archive = release_bundle
    (root / "src" / "spark_keeper" / "app.py").unlink()
    with pytest.raises(RuntimeError, match="构建来源"):
        publish["validate_archive"](archive, "3.0.0", root=root)


def test_legacy_manifest_remains_readable_but_cannot_be_newly_published(release_bundle):
    root, program, archive = release_bundle
    manifest = json.loads((program / "release-manifest.json").read_bytes())
    del manifest["build_provenance"]
    content = json.dumps(manifest).encode()
    (program / "release-manifest.json").write_bytes(content)
    # Runtime upgrade readers retain format 1 and the original files contract.
    installer_build["validate_manifest"](content, "3.0.0")
    installer_build["validate_payload"](program, "3.0.0")
    rewrite(archive, changed={"SparkKeeper/release-manifest.json": content})
    with pytest.raises(RuntimeError, match="构建来源"):
        publish["validate_archive"](archive, "3.0.0", root=root)


@pytest.mark.parametrize("change", ["old_source", "missing_provenance"])
def test_installer_cannot_relabel_old_payload(release_bundle, monkeypatch, change):
    root, program, _ = release_bundle
    if change == "old_source":
        (root / "src" / "spark_keeper" / "app.py").write_bytes(b"source B")
    else:
        manifest = json.loads((program / "release-manifest.json").read_bytes())
        del manifest["build_provenance"]
        (program / "release-manifest.json").write_text(json.dumps(manifest), "utf-8")
    receipt = root / "SparkKeeper-3.0.0-Setup.exe.build.json"
    before = receipt.read_bytes()
    globals_ = installer_build["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    compiler = Mock(side_effect=AssertionError("stale payload must fail before compilation"))
    monkeypatch.setitem(globals_, "find_iscc", compiler)
    assert installer_build["main"](["--payload", str(program), "--output-dir", str(root)]) == 1
    compiler.assert_not_called()
    assert receipt.read_bytes() == before


def test_installer_receipt_covers_exact_payload_provenance(release_bundle):
    root, program, _ = release_bundle
    installer = root / "SparkKeeper-3.0.0-Setup.exe"
    receipt_path = installer_build["receipt_path"](installer)
    receipt = json.loads(receipt_path.read_bytes())
    receipt["payload_build_provenance"]["toolchain"]["python"] = "another build"
    receipt_path.write_text(json.dumps(receipt), "utf-8")
    with pytest.raises(RuntimeError, match="构建凭据"):
        publish["validate_installer"](
            installer, "3.0.0", (program / "release-manifest.json").read_bytes(), root=root,
        )


def test_installer_source_change_during_compile_preserves_previous_artifacts(release_bundle, monkeypatch):
    root, program, _ = release_bundle
    globals_ = installer_build["main"].__globals__
    monkeypatch.setitem(globals_, "ROOT", root)
    monkeypatch.setitem(globals_, "find_iscc", lambda explicit: root / "ISCC.exe")
    artifacts = {path: path.read_bytes() for path in (
        root / "SparkKeeper-3.0.0-Setup.exe",
        root / "SparkKeeper-3.0.0-Setup.exe.sha256",
        root / "SparkKeeper-3.0.0-Setup.exe.build.json",
    )}

    def compile_(command, **kwargs):
        defines = dict(argument[2:].split("=", 1) for argument in command if argument.startswith("/D"))
        (Path(defines["OutputDir"]) / (defines["OutputBaseFilename"] + ".exe")).write_bytes(b"new fixture")
        (root / "src" / "spark_keeper" / "app.py").write_bytes(b"changed during compiler run")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(globals_["subprocess"], "run", compile_)
    assert installer_build["main"](["--payload", str(program), "--output-dir", str(root)]) == 1
    assert {path: path.read_bytes() for path in artifacts} == artifacts


@pytest.mark.parametrize("change_stage", ["compile", "archive", "unchanged"])
def test_portable_build_binds_only_unchanged_source_and_actual_command(release_bundle, monkeypatch, change_stage):
    root, program, _ = release_bundle
    globals_ = build["main"].__globals__
    python = root / ".venv" / "Scripts" / "python.exe"
    playwright = python.with_name("playwright.exe")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic python path; never executed")
    playwright.write_bytes(b"synthetic playwright path; never executed")
    browsers = root / ".venv" / "browsers"
    browsers.mkdir()
    for key, value in (("ROOT", root), ("PYTHON", python), ("PLAYWRIGHT", playwright),
                       ("LOCAL_BROWSERS", browsers)):
        monkeypatch.setitem(globals_, key, value)
    monkeypatch.setattr(globals_["sys"], "executable", str(python))
    monkeypatch.setitem(globals_, "copy_licenses",
                        lambda destination: shutil.copytree(program / "licenses", destination / "licenses"))
    commands = []

    def run_(command, **kwargs):
        commands.append(command)
        if "PyInstaller" not in command:
            return
        built = root / "work" / "pyinstaller" / "dist" / "SparkKeeper"
        for name in ("SparkKeeper.exe", "_internal/runtime.dll",
                     "_internal/PySide6/plugins/platforms/qwindows.dll",
                     "_internal/playwright/driver/package/.local-browsers/chrome.exe"):
            path = built / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic portable payload")
        if change_stage == "compile":
            (root / "src" / "spark_keeper" / "app.py").write_bytes(b"source B")

    monkeypatch.setitem(globals_, "run", run_)
    create_zip = build["create_zip"]

    def archive_(directory, archive, **kwargs):
        create_zip(directory, archive, **kwargs)
        if change_stage == "archive":
            (root / "src" / "spark_keeper" / "app.py").write_bytes(b"source B")

    monkeypatch.setitem(globals_, "create_zip", archive_)
    output = root / "outputs" / "release"
    manifest_path = output / "SparkKeeper" / "release-manifest.json"
    archive = output / "SparkKeeper-3.0.0-win64.zip"
    if change_stage == "unchanged":
        build["main"]()
        provenance = json.loads(manifest_path.read_bytes())["build_provenance"]
        assert provenance == build["build_provenance"](root)
        assert provenance["configuration"]["command"] == [
            argument.replace(str(root), "${ROOT}") for argument in commands[1]
        ]
        publish["validate_archive"](archive, "3.0.0", root=root)
    else:
        with pytest.raises(RuntimeError, match="构建期间"):
            build["main"]()
        assert not manifest_path.exists()
        assert not archive.exists()
        assert not archive.with_suffix(".zip.sha256").exists()
