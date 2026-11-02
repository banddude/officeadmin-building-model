"""Small source-only regressions for descriptor and growing-stream admission."""
import os
from pathlib import Path

import pytest
from oabm.model import ContractError
from oabm.reports.source_files import hash_source_file


def test_swapped_path_checks_the_descriptor_actually_opened(tmp_path,monkeypatch):
    source=tmp_path/'small';source.write_bytes(b'ab')
    replacement=tmp_path/'large';replacement.write_bytes(b'0123456789')
    original=os.open
    def swapped(path,flags):
        return original(replacement if Path(path)==source else path,flags)
    monkeypatch.setattr(os,'open',swapped)
    with pytest.raises(ContractError,match='descriptor'):
        hash_source_file(source,max_bytes=4)


def test_growing_opened_file_cannot_bypass_stream_byte_counter(tmp_path,monkeypatch):
    source=tmp_path/'growing';source.write_bytes(b'ab')
    original=os.fstat;grew=False
    def growing(descriptor):
        nonlocal grew
        info=original(descriptor)
        if not grew:
            grew=True
            with source.open('ab') as handle:handle.write(b'0123456789')
        return info
    monkeypatch.setattr(os,'fstat',growing)
    with pytest.raises(ContractError,match='stream'):
        hash_source_file(source,max_bytes=4)
