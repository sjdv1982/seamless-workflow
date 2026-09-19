"""Real-filesystem regressions for the mount/null decision tables."""
import gzip

import pytest

from seamless import Buffer, Cell, Checksum
from seamless_workflow import Context
from seamless_workflow.attachments import MountError


def test_initial_sensing_mount_without_value(tmp_path):
    path = tmp_path / "missing.txt"
    with Context() as ctx:
        ctx.value = Cell(celltype="text")
        ctx.value.mount(path, mode="r")
        assert ctx.value.state == "complete" and ctx.value.value is None
        assert not path.exists()


@pytest.mark.parametrize("empty", ["zero", "directory"])
def test_initial_empty_source_does_not_override_value(tmp_path, empty):
    path = tmp_path / ("folder" if empty == "directory" else "value.txt")
    if empty == "directory":
        path.mkdir()
        celltype = "deepfolder"
        leaf = Buffer(b"old"); leaf.tempref()
        value = {"a": leaf.get_checksum().hex()}
    else:
        path.write_bytes(b"")
        celltype, value = "text", "old"
    with Context() as ctx:
        ctx.value = Cell(celltype=celltype); ctx.value.set(value)
        ctx.value.mount(path, mode="r")
        assert ctx.value.value == value


def test_initial_file_and_strict_authority_rows(tmp_path):
    missing = tmp_path / "missing.txt"
    strict = tmp_path / "strict.txt"
    with Context() as ctx:
        ctx.file = Cell(celltype="text"); ctx.file.set("old")
        ctx.file.mount(missing, mode="rw", authority="file")
        assert missing.read_text() == "old\n"
        ctx.strict = Cell(celltype="text"); ctx.strict.set("kept")
        ctx.strict.mount(strict, authority="file-strict")
        assert ctx.strict.state == "failed"
        assert isinstance(ctx.strict.exception, MountError)


def test_set_graph_mount_uses_same_missing_file_policy(tmp_path):
    path = tmp_path / "value.txt"
    with Context() as source:
        source.value = Cell(celltype="text"); source.value.set("stored")
        source.value.mount(path); graph = source.get_graph()
    path.unlink()
    with Context() as restored:
        restored.set_graph(graph, mounts=True)
        assert restored.value.value == "stored"
        assert path.read_text() == "stored\n"


def test_strict_missing_without_cell_value_stores_hidden_null(tmp_path):
    path = tmp_path / "strict.txt"
    with Context() as ctx:
        ctx.value = Cell(celltype="text"); ctx.value.mount(path, authority="file-strict")
        assert ctx.value.state == "failed" and isinstance(ctx.value.exception, MountError)
        graph_value = ctx.get_graph()["nodes"][0]["value"]["checksum"]
        from seamless.checksum.null import NULL_CHECKSUM
        assert graph_value == Checksum(NULL_CHECKSUM).hex()
        path.write_text("recovered"); ctx.mounts.sync(timeout=5)
        assert ctx.value.value == "recovered"


def test_initial_explicit_null_wins_for_file_authority(tmp_path):
    path = tmp_path / "value.json"; path.write_text("null\n")
    with Context() as ctx:
        ctx.value = Cell(celltype="plain"); ctx.value.set({"old": True})
        ctx.value.mount(path, authority="file")
        assert ctx.value.value is None


def test_initial_cell_authority_rows(tmp_path):
    read_path = tmp_path / "read.txt"; read_path.write_text("file")
    write_path = tmp_path / "write.txt"; write_path.write_text("null\n")
    with Context() as ctx:
        ctx.read = Cell(celltype="text"); ctx.read.set("cell")
        ctx.read.mount(read_path, mode="r", authority="cell")
        assert ctx.read.value == "cell" and read_path.read_text() == "file"
        ctx.write = Cell(celltype="text"); ctx.write.set("cell")
        ctx.write.mount(write_path, mode="rw", authority="cell")
        assert write_path.read_text() == "cell\n"


def test_initial_write_only_rows(tmp_path):
    incomplete_path = tmp_path / "incomplete.txt"; incomplete_path.write_text("file")
    value_path = tmp_path / "value.txt"; value_path.write_text("file")
    with Context() as ctx:
        ctx.incomplete = Cell(celltype="text")
        ctx.incomplete.mount(incomplete_path, mode="w")
        assert incomplete_path.read_text() == "file"
        ctx.value = Cell(celltype="text"); ctx.value.set("cell")
        ctx.value.mount(value_path, mode="w")
        assert value_path.read_text() == "cell\n"


def test_deleted_sensing_file_preserves_value_and_strict_fails(tmp_path):
    normal = tmp_path / "normal.txt"; normal.write_text("file")
    strict = tmp_path / "strict.txt"; strict.write_text("file")
    with Context() as ctx:
        ctx.normal = Cell(celltype="text"); ctx.normal.mount(normal)
        normal.unlink(); ctx.mounts.sync(timeout=5)
        assert ctx.normal.value == "file"
        ctx.strict = Cell(celltype="text"); ctx.strict.mount(strict, authority="file-strict")
        strict.unlink(); ctx.mounts.sync(timeout=5)
        assert ctx.strict.state == "failed" and isinstance(ctx.strict.exception, MountError)


def test_file_changes_empty_valid_and_invalid(tmp_path):
    path = tmp_path / "value.json"; path.write_text('{"a": 1}')
    with Context() as ctx:
        ctx.value = Cell(celltype="plain"); ctx.value.mount(path)
        path.write_bytes(b""); ctx.mounts.sync(timeout=5)
        assert ctx.value.value is None
        path.write_text('{"a": 2}'); ctx.mounts.sync(timeout=5)
        assert ctx.value.value == {"a": 2}
        path.write_text("broken"); ctx.mounts.sync(timeout=5)
        assert ctx.value.state == "failed" and isinstance(ctx.value.exception, MountError)
        path.unlink(); ctx.mounts.sync(timeout=5)
        assert ctx.value.value == {"a": 2}


@pytest.mark.parametrize("content", [b"", b"foreign", b"\xff"])
def test_write_mount_reasserts_file_changes(tmp_path, content):
    path = tmp_path / "value.txt"; path.write_text("old")
    with Context() as ctx:
        ctx.value = Cell(celltype="text"); ctx.value.set("cell")
        ctx.value.mount(path, mode="w")
        path.write_bytes(content); ctx.mounts.sync(timeout=5)
        assert path.read_text() == "cell\n"


def test_null_cell_writes_empty_but_does_not_create(tmp_path):
    existing = tmp_path / "existing.txt"; existing.write_text("old")
    missing = tmp_path / "missing.txt"
    with Context() as ctx:
        ctx.existing = Cell(celltype="text"); ctx.existing.set("old")
        ctx.existing.mount(existing, mode="w"); ctx.existing.set(None)
        ctx.mounts.sync(timeout=5); assert existing.read_bytes() == b""
        ctx.missing = Cell(celltype="text"); ctx.missing.set(None)
        ctx.missing.mount(missing, mode="w"); ctx.mounts.sync(timeout=5)
        assert not missing.exists()


@pytest.mark.parametrize("suffix", [".gz", ".zst"])
def test_null_cell_writes_zero_byte_compressed_file(tmp_path, suffix):
    if suffix == ".zst": pytest.importorskip("zstandard")
    path = tmp_path / ("value" + suffix)
    encoded = gzip.compress(b"old") if suffix == ".gz" else __import__("zstandard").ZstdCompressor().compress(b"old")
    path.write_bytes(encoded)
    with Context() as ctx:
        ctx.value = Cell(celltype="text"); ctx.value.set("old")
        ctx.value.mount(path, mode="w"); ctx.value.set(None)
        ctx.mounts.sync(timeout=5)
        assert path.read_bytes() == b""


def test_null_directory_keeps_tree_and_is_out_of_sync(tmp_path):
    path = tmp_path / "folder"; path.mkdir(); (path / "a").write_bytes(b"content")
    with Context() as ctx:
        ctx.value = Cell(celltype="folder"); ctx.value.mount(path, mode="rw")
        ctx.value.set(None); report = ctx.mounts.sync(timeout=5)
        assert (path / "a").read_bytes() == b"content"
        assert not report[("value",)]["in_sync"]
