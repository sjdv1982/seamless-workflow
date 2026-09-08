from seamless import Buffer, diagnostics
from seamless_workflow import Context


def test_recording_exact_buffer_roundtrip():
    with diagnostics.record_materialisation() as log:
        buf = Buffer(314159, 'plain')
        checksum = buf.get_checksum()
        assert checksum.resolve('plain') == 314159
    assert log.ops == [
        ('serialize', None, 'plain', None),
        ('hash', None, None, len(buf)),
        ('resolve', checksum.hex(), 'plain', None),
        ('deserialize', checksum.hex(), 'plain', len(buf)),
    ]


def test_recording_disabled_by_default():
    with diagnostics.record_materialisation() as log:
        pass
    Buffer(123, 'plain').get_checksum()
    assert log.ops == []
