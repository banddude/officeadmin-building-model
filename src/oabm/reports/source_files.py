"""Bounded descriptor-based hashing for explicitly supplied local source files."""
import hashlib
import os
import stat

from oabm.model import ContractError

SOURCE_FILE_BYTES = 256*1024*1024


def _read_chunks(path,*,max_bytes,consume):
    """Bound the opened descriptor and every streamed byte, including growth."""
    if type(max_bytes) is not int or max_bytes<1:raise ContractError('source byte limit must be positive')
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(descriptor,'rb') as handle:
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>max_bytes:
            raise ContractError('source descriptor must be a regular file within byte admission')
        count=0
        while True:
            block=handle.read(min(1024*1024,max_bytes-count+1))
            if not block:break
            count+=len(block)
            if count>max_bytes:raise ContractError('source stream exceeds byte admission')
            consume(block)
        after=os.fstat(handle.fileno())
        if (after.st_size!=count or before.st_size!=after.st_size or
                before.st_mtime_ns!=after.st_mtime_ns):
            raise ContractError('source descriptor changed while hashing')


def hash_source_file(path,*,max_bytes=SOURCE_FILE_BYTES):
    digest=hashlib.sha256()
    _read_chunks(path,max_bytes=max_bytes,consume=digest.update)
    return digest.hexdigest()


def read_source_bytes(path,*,max_bytes):
    """Nonblocking-open regular-file capture for bounded JSON/asset admission."""
    chunks=[]
    _read_chunks(path,max_bytes=max_bytes,consume=chunks.append)
    return b''.join(chunks)
