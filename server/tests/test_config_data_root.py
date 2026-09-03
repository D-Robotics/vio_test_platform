"""Regression test for DATA_ROOT canonicalization.

DATA_ROOT is often passed as a ``..``/relative path (e.g.
``.../vio_test_platform/../vio_data``). The NFS server matches a board mount to an
export name literally on the wire (it does not collapse ``..``), so an un-canonical
DATA_ROOT makes the mount fail with "access denied". ``canonical_data_root`` must
resolve it so the export and the mount share one path.
"""
from server import config


def test_canonical_data_root_collapses_dotdot():
    assert config.canonical_data_root("/a/b/../c") == "/a/c"
    assert ".." not in config.canonical_data_root("/a/b/../c")


def test_canonical_data_root_keeps_normal_path():
    assert config.canonical_data_root("/mnt/newdisk/huaiyu.zhang/vio_data") == "/mnt/newdisk/huaiyu.zhang/vio_data"


def test_canonical_data_root_still_works_for_missing_dir():
    # the dir need not exist; .. is resolved lexically
    assert config.canonical_data_root("/tmp/nope/../also_nope") == "/tmp/also_nope"
