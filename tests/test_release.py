"""Release numbering, distributable content, retries and concurrent main pushes."""

import json
import subprocess
from zipfile import ZipFile

import pytest
import yaml

from scripts.release import MANIFEST, PROJECT, ROOT, build, bump, git, prepare, version


def run(*args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    run("git", "init", "-b", "main", cwd=root)
    for key, value in (
        ("user.name", "Release test"),
        ("user.email", "release@example.test"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git("config", key, value, root=root)
    (root / MANIFEST).parent.mkdir(parents=True)
    (root / MANIFEST).write_text(
        json.dumps({"domain": "zonentra", "version": "0.13.6"})
    )
    (root / PROJECT).write_text('[project]\nname = "zonentra"\nversion = "0.13.6"\n')
    (root / MANIFEST.parent / "__init__.py").write_text('"""Integration."""\n')
    (root / "README.md").write_text("Not part of the installed integration.\n")
    git("add", ".", root=root)
    git("commit", "-m", "Initial tested source", root=root)
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), root=root)
    git("push", "origin", "main", root=root)
    return root


@pytest.mark.parametrize(
    "part,expected", [("patch", "0.13.7"), ("minor", "0.14.0"), ("major", "1.0.0")]
)
def test_bump_keeps_both_versions_identical(repository, part, expected):
    assert bump(part, repository) == expected
    assert version(repository) == expected
    assert 'name = "zonentra"' in (repository / PROJECT).read_text()


def test_mismatched_version_is_rejected_before_writing(repository):
    (repository / PROJECT).write_text('[project]\nversion = "0.12.0"\n')
    original = (repository / MANIFEST).read_bytes()
    with pytest.raises(ValueError, match="versions differ"):
        bump("patch", repository)
    assert (repository / MANIFEST).read_bytes() == original


@pytest.mark.parametrize("value", ["0.01.0", "0.14.0.dev1", "1.0", "v1.0.0", 1])
def test_release_versions_are_stable_and_unambiguous(repository, value):
    (repository / MANIFEST).write_text(json.dumps({"version": value}))
    with pytest.raises(ValueError, match="major.minor.patch"):
        version(repository)


def test_build_contains_only_tracked_integration_files(repository):
    (repository / MANIFEST.parent / "private.json").write_text('"not tracked"')
    cache = repository / MANIFEST.parent / "__pycache__"
    cache.mkdir()
    (cache / "old.pyc").write_bytes(b"not distributable")
    assert build(repository) == "0.13.6"
    package = repository / "dist/zonentra.zip"
    original = package.read_bytes()
    with ZipFile(package) as archive:
        assert sorted(archive.namelist()) == ["__init__.py", "manifest.json"]
        assert json.loads(archive.read("manifest.json"))["version"] == "0.13.6"
    metadata = json.loads((repository / "dist/build-info.json").read_text())
    assert metadata["commit"] == git("rev-parse", "HEAD", root=repository)
    assert build(repository) == "0.13.6"
    assert package.read_bytes() == original


def test_releases_are_monotonic_atomic_and_repeatable(repository):
    source = git("rev-parse", "HEAD", root=repository)
    assert prepare(source, "123", root=repository) == "0.13.7"
    release = git("rev-parse", "HEAD", root=repository)
    assert version(repository) == "0.13.7"
    assert git("rev-parse", "0.13.7^{commit}", root=repository) == release
    assert git("rev-parse", "origin/main", root=repository) == release
    assert git(
        "show", "--format=", "--name-only", "HEAD", root=repository
    ).splitlines() == [str(MANIFEST), str(PROJECT)]
    # A failed GitHub publish is retried from a fresh checkout of the tested SHA.
    git("checkout", "--detach", source, root=repository)
    assert prepare(source, "123", root=repository) == "0.13.7"
    assert git("rev-parse", "HEAD", root=repository) == release
    # A different old workflow must not publish this run's release.
    assert prepare(source, "124", root=repository) is None
    # A subsequent tested push receives exactly the next patch.
    git("checkout", "main", root=repository)
    (repository / "README.md").write_text("Next change.\n")
    git("add", "README.md", root=repository)
    git("commit", "-m", "Next tested change", root=repository)
    git("push", "origin", "main", root=repository)
    assert (
        prepare(git("rev-parse", "HEAD", root=repository), "125", root=repository)
        == "0.13.8"
    )


def test_newer_main_skips_obsolete_successful_build(repository):
    old_source = git("rev-parse", "HEAD", root=repository)
    (repository / "README.md").write_text("A newer untested change.\n")
    git("add", "README.md", root=repository)
    git("commit", "-m", "Newer main", root=repository)
    git("push", "origin", "main", root=repository)
    git("checkout", "--detach", old_source, root=repository)
    assert prepare(old_source, "123", root=repository) is None
    assert version(repository) == "0.13.6"
    assert git("tag", root=repository) == ""


def test_push_race_does_not_publish_an_orphan_tag(repository, monkeypatch):
    from scripts import release

    source = git("rev-parse", "HEAD", root=repository)
    original_git = release.git
    injected = False

    def raced_git(*args, root):
        nonlocal injected
        if args[0] == "push" and not injected:
            injected = True
            # Another developer pushes a descendant of the tested source.
            head = original_git("rev-parse", "HEAD", root=root)
            original_git("checkout", "--detach", source, root=root)
            original_git("commit", "--allow-empty", "-m", "Concurrent push", root=root)
            original_git("push", "origin", "HEAD:main", root=root)
            original_git("checkout", "--detach", head, root=root)
        return original_git(*args, root=root)

    monkeypatch.setattr(release, "git", raced_git)
    with pytest.raises(subprocess.CalledProcessError):
        prepare(source, "123", root=repository)
    assert "refs/tags/0.13.7" not in git(
        "ls-remote", "--tags", "origin", root=repository
    )


def test_reused_or_decreasing_tag_is_refused(repository):
    git("tag", "v0.14.0", root=repository)
    source = git("rev-parse", "HEAD", root=repository)
    with pytest.raises(ValueError, match="higher than"):
        prepare(source, "123", root=repository)
    assert git("rev-parse", "origin/main", root=repository) == source


def test_retry_requires_matching_tag(repository):
    source = git("rev-parse", "HEAD", root=repository)
    prepare(source, "123", root=repository)
    git("tag", "-f", "0.13.7", source, root=repository)
    git("push", "--force", "origin", "refs/tags/0.13.7", root=repository)
    with pytest.raises(ValueError, match="tag does not match"):
        prepare(source, "123", root=repository)


def test_workflow_only_releases_tested_main_and_serializes_publication():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["release"]["needs"] == "test"
    assert (
        jobs["release"]["if"]
        == "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'"
    )
    assert jobs["release"]["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert version(ROOT)
