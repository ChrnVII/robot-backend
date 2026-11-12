"""沙箱逃逸测试矩阵。

这是整个系统最该被测死的地方。每个用例的期望都是"被拒绝且抛 SandboxError"，
而不是"返回了一个奇怪的路径"。
"""

from __future__ import annotations

import os

import pytest

from app.cloud_agent import Sandbox, SandboxError, is_within

pytestmark = pytest.mark.no_database


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "share"
    r.mkdir()
    (r / "a.txt").write_text("hello", encoding="utf-8")
    (r / "报表 2026.xlsx").write_text("x", encoding="utf-8")
    sub = r / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world", encoding="utf-8")
    return r


@pytest.fixture()
def box(root):
    return Sandbox(root)


def test_empty_rel_is_root(box, root):
    assert box.resolve("") == root.resolve()


def test_simple_file(box):
    assert box.resolve("a.txt").name == "a.txt"


def test_chinese_filename(box):
    assert box.resolve("报表 2026.xlsx").name == "报表 2026.xlsx"


def test_nested_file(box):
    assert box.resolve("sub/b.txt").name == "b.txt"


def test_backslash_is_an_ordinary_filename_char(box):
    """POSIX 上反斜杠是合法的文件名字符，不是路径分隔符。"""
    assert box.normalize_rel("sub\\b.txt") == "sub\\b.txt"


def test_redundant_separators_collapse(box):
    assert box.normalize_rel("sub//b.txt") == "sub/b.txt"
    assert box.normalize_rel("./sub/./b.txt") == "sub/b.txt"


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "sub/../../secret",
        "..",
        "sub/..",
        "a/../b",
        "./sub/../../../x",
    ],
)
def test_rejects_traversal(box, bad):
    with pytest.raises(SandboxError, match="traversal"):
        box.resolve(bad)


@pytest.mark.parametrize("bad", ["/etc/passwd", "//server/share/x", "/"])
def test_rejects_absolute_paths(box, bad):
    with pytest.raises(SandboxError):
        box.resolve(bad)


def test_rejects_null_byte(box):
    with pytest.raises(SandboxError, match="null byte"):
        box.resolve("a\x00b")


def test_rejects_overly_deep_path(box):
    deep = "/".join(["d"] * 200)
    with pytest.raises(SandboxError, match="too deep"):
        box.resolve(deep)


def test_rejects_overly_long_path(box):
    long_name = "a" * 5000
    with pytest.raises(SandboxError, match="too long"):
        box.resolve(long_name)


def test_rejects_non_string(box):
    with pytest.raises(SandboxError, match="must be a string"):
        box.resolve(123)  # type: ignore[arg-type]


def test_symlink_escape_is_rejected(tmp_path, root):
    """根目录内一个指向外部的软链接，是 resolve() 之前做检查就会漏掉的情形。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不支持创建软链接，无法验证该路径")

    if not os.path.islink(link):
        pytest.skip("当前文件系统未真正创建链接，无法验证该路径")

    box = Sandbox(root)
    with pytest.raises(SandboxError, match="escapes root"):
        box.resolve("link/secret.txt")


def test_is_within_accepts_nested(root):
    assert is_within(root / "sub" / "b.txt", root)
    assert is_within(root, root)


def test_is_within_rejects_sibling_with_shared_prefix(tmp_path, root):
    """`share2` 不能因为和 `share` 有公共前缀就被判为内部。"""
    sibling = tmp_path / "share2"
    sibling.mkdir()
    assert not is_within(sibling, root)
    assert not is_within(sibling / "x.txt", root)


def test_is_within_rejects_parent(root):
    assert not is_within(root.parent, root)


def test_rejects_missing_root(tmp_path):
    with pytest.raises(SandboxError):
        Sandbox(tmp_path / "does-not-exist")


def test_rejects_empty_root():
    with pytest.raises(SandboxError):
        Sandbox("")
