from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MakeRepo, git

from revgate.gitio import (
    GitError,
    SafetyError,
    blob_sha,
    cat_blobs,
    diff_hunks,
    diff_name_status,
    diff_numstat,
    is_ancestor,
    ls_tree,
    merge_base,
    rev_parse,
    safe_toplevel,
    show,
    status_porcelain,
)

BODY = "".join(f"line {i}\n" for i in range(1, 21))


def test_diff_name_status_reports_a_rename_add_modify_and_delete(make_repo: MakeRepo) -> None:
    repo = make_repo(
        {"old name.py": BODY, "keep.py": "a\n", "gone.py": "g\n"},
        {
            "old name.py": None,
            "new name.py": BODY,
            "keep.py": "b\n",
            "gone.py": None,
            "added.py": "n\n",
        },
    )
    got = sorted(diff_name_status(repo.path, repo.base, repo.head))
    assert got == [
        ("A", "added.py", None),
        ("D", "gone.py", None),
        ("M", "keep.py", None),
        ("R", "new name.py", "old name.py"),
    ]


def test_diff_numstat_counts_lines_and_uses_the_new_path(make_repo: MakeRepo) -> None:
    repo = make_repo(
        {"a.py": "1\n2\n", "r.py": BODY},
        {"a.py": "1\n3\n4\n", "r.py": None, "moved.py": BODY + "extra\n"},
    )
    (repo.path / "img.bin").write_bytes(b"\x00\x01\x02")
    git(repo.path, "add", "img.bin")
    git(repo.path, "commit", "-q", "-m", "bin")
    head3 = git(repo.path, "rev-parse", "HEAD")
    stats = {p: (a, d) for a, d, p in diff_numstat(repo.path, repo.base, repo.head)}
    assert stats["a.py"] == (2, 1)
    assert stats["moved.py"] == (1, 0)
    assert "r.py" not in stats
    binary = {p: (a, d) for a, d, p in diff_numstat(repo.path, repo.head, head3)}
    assert binary["img.bin"] == (None, None)


def test_diff_hunks_gives_head_side_ranges(make_repo: MakeRepo) -> None:
    head_body = BODY.replace("line 3\n", "LINE 3\nline 3b\n").replace("line 10\n", "")
    repo = make_repo(
        {"m.py": BODY, "d.py": "x\n"},
        {"m.py": head_body + "+++ b/fake\n", "d.py": None, "n.py": "a\nb\n"},
    )
    hunks = diff_hunks(repo.path, repo.base, repo.head)
    assert hunks["m.py"] == ((3, 2), (10, 0), (21, 1))
    assert hunks["n.py"] == ((1, 2),)
    assert "d.py" not in hunks
    assert "fake" not in hunks


def test_diff_hunks_unquotes_non_ascii_and_spaced_paths(make_repo: MakeRepo) -> None:
    repo = make_repo({}, {"caf\u00e9.py": "a\n", "with space.py": "b\nc\n"})
    hunks = diff_hunks(repo.path, repo.base, repo.head)
    assert hunks == {"caf\u00e9.py": ((1, 1),), "with space.py": ((1, 2),)}


def test_ls_tree_and_cat_blobs_round_trip_bytes(make_repo: MakeRepo) -> None:
    repo = make_repo({"dir/a b.txt": "spaced\n", "c.py": "print(1)\n"}, {})
    tree = ls_tree(repo.path, repo.head)
    assert set(tree) == {"dir/a b.txt", "c.py"}
    blobs = cat_blobs(repo.path, [tree["dir/a b.txt"], tree["c.py"], tree["c.py"]])
    assert blobs[tree["dir/a b.txt"]] == b"spaced\n"
    assert blobs[tree["c.py"]] == b"print(1)\n"
    assert cat_blobs(repo.path, []) == {}
    assert blob_sha(repo.path, repo.head, "c.py") == tree["c.py"]
    assert blob_sha(repo.path, repo.head, "missing.py") is None
    assert blob_sha(repo.path, repo.head, "dir") is None


def test_show_returns_bytes_or_none(make_repo: MakeRepo) -> None:
    repo = make_repo({"a.py": "one\n"}, {"a.py": "two\n"})
    assert show(repo.path, repo.base, "a.py") == b"one\n"
    assert show(repo.path, repo.head, "a.py") == b"two\n"
    assert show(repo.path, repo.head, "nope.py") is None
    with pytest.raises(GitError):
        show(repo.path, "0" * 40, "a.py")


def test_rev_parse_ancestry_and_merge_base(make_repo: MakeRepo) -> None:
    repo = make_repo({"a.py": "1\n"}, {"a.py": "2\n"})
    assert rev_parse(repo.path, "HEAD") == repo.head
    assert len(rev_parse(repo.path, "HEAD~1")) == 40
    assert is_ancestor(repo.path, repo.base, repo.head)
    assert not is_ancestor(repo.path, repo.head, repo.base)
    assert merge_base(repo.path, repo.base, repo.head) == repo.base
    with pytest.raises(GitError):
        rev_parse(repo.path, "no-such-branch")
    with pytest.raises(GitError):
        rev_parse(repo.path, "--all")


def test_status_porcelain_lists_untracked_and_modified(make_repo: MakeRepo) -> None:
    repo = make_repo({"a.py": "1\n", "b.py": "b\n"}, {})
    (repo.path / "a.py").write_text("changed\n")
    (repo.path / "new dir").mkdir()
    (repo.path / "new dir" / "u.txt").write_text("u\n")
    git(repo.path, "mv", "b.py", "c.py")
    status = status_porcelain(repo.path)
    assert (" M", "a.py") in status
    assert ("??", "new dir/u.txt") in status
    assert ("R ", "c.py") in status
    assert all(path != "b.py" for _, path in status)


def test_safe_toplevel_refuses_home(make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo({"a.py": "1\n"}, {})
    assert safe_toplevel(repo.path) == repo.path.resolve()
    monkeypatch.setenv("HOME", str(repo.path))
    with pytest.raises(SafetyError):
        safe_toplevel(repo.path)


def test_outside_a_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SafetyError):
        safe_toplevel(tmp_path)
