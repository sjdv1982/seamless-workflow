"""Deterministic archive packing for share downloads.

The archive bytes depend only on the members and the format (for a given
compression library): members are sorted, and every timestamp, owner and mode
is fixed.  The archive is still never content-addressed.
"""

import gzip
import io
import lzma
import tarfile
import zipfile


_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def is_safe_member_path(name):
    """Whether a folder index key can be placed below a member directory."""
    return (
        isinstance(name, str)
        and bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in name.split("/"))
    )


def pack(format, members):
    """Pack ``(name, bytes)`` members into an archive of ``format``."""
    members = sorted(members)
    output = io.BytesIO()
    if format == "zip":
        with zipfile.ZipFile(output, "w") as bundle:
            for name, data in members:
                info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, data)
        return output.getvalue()
    if format == "tar.gz":
        stream = gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0)
    elif format == "tar.xz":
        stream = lzma.LZMAFile(output, "wb")
    elif format == "tar":
        stream = output
    else:
        raise ValueError(f"unknown download format {format!r}")
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            bundle.addfile(info, io.BytesIO(data))
    if stream is not output:
        stream.close()
    return output.getvalue()
