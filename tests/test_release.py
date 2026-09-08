from __future__ import annotations

import hashlib
import json
import runpy
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
build = runpy.run_path(str(ROOT / "tools" / "build_portable.py"))
publish = runpy.run_path(str(ROOT / "tools" / "publish_release.py"))


@pytest.fixture
def release_bundle(tmp_path):
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
    (tools / "update.cmd").write_bytes(b"@echo off\r\n")
    (tools / "update.ps1").write_bytes(b"Write-Output fixture\r\n")
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
    build["write_release_manifest"](program, "3.0.0")
    archive = tmp_path / "SparkKeeper-3.0.0-win64.zip"
    build["create_zip"](program, archive, updater_directory=tools)
    checksum(archive)
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
    build["write_release_manifest"](program, version)
    archive = final_archive.with_name("SparkKeeper-3.0.1-rc.1-win64.zip")
    build["create_zip"](program, archive, updater_directory=root / "tools")
    checksum(archive)
    assert json.loads((program / "release-manifest.json").read_text("utf-8"))["version"] == version
    publish["validate_archive"](archive, version, root=root)
    fake_git(monkeypatch)
    assert publish["validate_git"](root, "owner/repo", "v3.0.1-rc.1", version) == "abc"
    with pytest.raises(ValueError, match="v3.0.1-rc.1"):
        publish["validate_git"](root, "owner/repo", "v3.0.1rc1", version)
