"""MountLease keeps its own role (it used to be overwritten with "snapshot")."""

from seamless import Buffer


def test_mountlease_keeps_its_role():
    from seamless_workflow.attachments.session import MountLease

    buf = Buffer(b"mountlease-role")
    lease = MountLease(buf.get_checksum(), "mount:node/a")
    try:
        assert lease.role == "mount:node/a"
        assert lease._refheld_checksums() == ((buf.get_checksum(), "mount:node/a"),)
    finally:
        lease._release_refholds()
