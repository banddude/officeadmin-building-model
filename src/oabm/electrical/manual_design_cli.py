"""Offline, opt-in manual intake and preliminary design CLI.

Caller-supplied source, frame, grouping and placement are not authenticated or
verified. File publication is create-only; output may contain private model facts.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import stat
import sys

from oabm.electrical.manual_device_intake import admit_manual_devices, manual_intake_request_sha256
from oabm.electrical.manual_preliminary_design import design_manual_intake, manual_design_decision_sha256, _REPORT_SCHEMA
from oabm.model import BuildingModel
from oabm.reports.source_coverage import model_fingerprint
from oabm.routing.physical_capture import bounded_source_json

_INPUT_LIMIT = 1024 * 1024
_OUTPUT_LIMIT = 8 * 1024 * 1024
_REQUEST_FIELDS = frozenset({'request_id', 'source_sha256', 'base_model_sha256', 'frame_id', 'items'})


class _PublishedCleanupError(Exception):
    """Final output exists, but its private temporary link could not be removed."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo user-supplied values or private paths.
        self.print_usage(sys.stderr)
        self.exit(2, 'Invalid command arguments.\n')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('invalid JSON number')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('invalid JSON number')
    return parsed


def _read_text(path, *, byte_limit=_INPUT_LIMIT):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit:
            raise ValueError('input is not a bounded regular file')
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            data = handle.read(byte_limit + 1)
            after = os.fstat(handle.fileno())
        if (len(data) > byte_limit or len(data) != before.st_size or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('input changed during reading or is too large')
        text = bounded_source_json(data)
        # Scan duplicate keys and non-finite numbers before canonical decoding.
        json.loads(text, object_pairs_hook=_unique_object,
                   parse_constant=_reject_constant, parse_float=_finite_float)
        return text
    finally:
        if fd is not None:
            os.close(fd)


def _read_json(path):
    return json.loads(_read_text(path), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=_finite_float)


def _read_model(path):
    # Preserve the canonical model's own decoding and physical-number rules.
    return BuildingModel.from_json(_read_text(path, byte_limit=_OUTPUT_LIMIT))


def _bundle_model_text(text):
    """Read the model's original JSON substring before canonical decoding.

    Keeping the exact numeric tokens lets BuildingModel.from_json apply its
    physical-number rules instead of validating a float-rounded rewrite. The
    included fingerprint detects mismatch only; it is not authentication.
    """
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant, parse_float=_finite_float)
    document = decoder.decode(text)
    if (not isinstance(document, dict) or set(document) != {'version', 'designed_model', 'report'}
        or document['version'] != 'manual-preliminary-design-cli-v1'
        or not isinstance(document['report'], dict)):
        raise ValueError('invalid design bundle')
    _REPORT_SCHEMA.validate(document['report'])
    def skip(index):
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        return index
    index = skip(0)
    if index >= len(text) or text[index] != '{':
        raise ValueError('invalid design bundle')
    index += 1
    raw_model = None
    while True:
        index = skip(index)
        if index < len(text) and text[index] == '}':
            break
        key, index = decoder.raw_decode(text, index)
        index = skip(index)
        if not isinstance(key, str) or index >= len(text) or text[index] != ':':
            raise ValueError('invalid design bundle')
        start = skip(index + 1)
        _, end = decoder.raw_decode(text, start)
        if key == 'designed_model':
            raw_model = text[start:end]
        index = skip(end)
        if index < len(text) and text[index] == ',':
            index += 1
            continue
        if index < len(text) and text[index] == '}':
            break
        raise ValueError('invalid design bundle')
    if raw_model is None:
        raise ValueError('invalid design bundle')
    model = BuildingModel.from_json(raw_model)
    if document['report'].get('designed_model_sha256') != model_fingerprint(model):
        raise ValueError('design bundle model mismatch')
    return model


def _bundle_model(path):
    return _bundle_model_text(_read_text(path, byte_limit=_OUTPUT_LIMIT))


def _request(path, *, with_digest):
    value = _read_json(path)
    expected = _REQUEST_FIELDS | ({'request_sha256'} if with_digest else set())
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('invalid intake fields')
    return value


def _open_output_parent(path):
    parent, name = os.path.split(path)
    if not name or name in ('.', '..'):
        raise ValueError('invalid output name')
    fd = os.open(parent or '.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    try:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return fd, name
        raise FileExistsError('output exists')
    except BaseException:
        os.close(fd)
        raise


def _publish_json(parent_fd, name, value, *, verify_bundle=False):
    """Publish one complete 0600-or-more-restrictive file without replacement.

    Hard-link creation is atomic and fails if the destination exists, including
    a dangling symlink. Both entries use the same pinned caller-chosen parent.
    Unsupported filesystems fail closed. Crash durability is not asserted.
    """
    temporary = '.oabm-manual-' + secrets.token_hex(16) + '.tmp'
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
    published = False
    try:
        with os.fdopen(fd, 'w+b') as handle:
            fd = None
            encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            total = 0
            for chunk in encoder.iterencode(value):
                data = chunk.encode('ascii')
                total += len(data)
                if total + 1 > _OUTPUT_LIMIT:
                    raise ValueError('output exceeds size limit')
                handle.write(data)
            handle.write(b'\n')
            handle.flush()
            handle.seek(0)
            # Do not publish output that this CLI cannot read back because
            # wrapping made a formerly valid object too deep or too large.
            text = bounded_source_json(handle.read(_OUTPUT_LIMIT + 1))
            if verify_bundle:
                _bundle_model_text(text)
            os.fsync(handle.fileno())
        os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        published = True
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if published:
                raise _PublishedCleanupError from exc
            raise


def _parser():
    parser = _Parser(description='Local manual-device intake and preliminary design. No source, field, engineering or code verification is implied.', allow_abbrev=False)
    sub = parser.add_subparsers(dest='mode', required=True, parser_class=_Parser)
    model_digest = sub.add_parser('model-digest', help='print a canonical model fingerprint', allow_abbrev=False)
    model_digest.add_argument('--model', required=True)
    intake_digest = sub.add_parser('intake-digest', help='print an intake request correlation digest', allow_abbrev=False)
    intake_digest.add_argument('--intake', required=True)
    prepare = sub.add_parser('prepare', help='recompute intake and write a private report', allow_abbrev=False)
    prepare.add_argument('--model', required=True)
    prepare.add_argument('--intake', required=True)
    prepare.add_argument('--output', required=True)
    decision_digest = sub.add_parser('decision-digest', help='print a decision correlation digest', allow_abbrev=False)
    decision_digest.add_argument('--decision', required=True)
    extract = sub.add_parser('extract-model', help='write a standalone model from a consistent design bundle', allow_abbrev=False)
    extract.add_argument('--bundle', required=True)
    extract.add_argument('--output', required=True)
    design = sub.add_parser('design', help='write one private preliminary model and report bundle', allow_abbrev=False)
    design.add_argument('--model', required=True)
    design.add_argument('--intake', required=True)
    design.add_argument('--decision', required=True)
    design.add_argument('--decision-sha256', required=True)
    design.add_argument('--output', required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    output_fd = None
    try:
        if args.mode == 'model-digest':
            print(model_fingerprint(_read_model(args.model)))
        elif args.mode == 'intake-digest':
            print(manual_intake_request_sha256(**_request(args.intake, with_digest=False)))
        elif args.mode == 'decision-digest':
            print(manual_design_decision_sha256(_read_json(args.decision)))
        elif args.mode == 'extract-model':
            output_fd, output_name = _open_output_parent(args.output)
            _publish_json(output_fd, output_name, _bundle_model(args.bundle).to_dict())
        else:
            # Check before expensive work; the later link is the atomic
            # no-replacement check if another process races this test.
            output_fd, output_name = _open_output_parent(args.output)
            model = _read_model(args.model)
            intake = _request(args.intake, with_digest=True)
            if args.mode == 'prepare':
                _, receipt = admit_manual_devices(model, **intake)
                output = {'version': 'manual-intake-preparation-cli-v1', 'report': receipt}
            else:
                decision = _read_json(args.decision)
                result, report = design_manual_intake(model, intake_request=intake, decision=decision,
                                                       decision_sha256=args.decision_sha256)
                output = {'version': 'manual-preliminary-design-cli-v1',
                          'designed_model': result.to_dict(), 'report': report}
            _publish_json(output_fd, output_name, output, verify_bundle=args.mode == 'design')
        return 0
    except _PublishedCleanupError:
        print('Output was created, but private temporary-file cleanup failed. Inspect the destination and directory.', file=sys.stderr)
        return 3
    except Exception:
        # Do not leak input data, filesystem paths or exceptions. If failure
        # occurred after publication, the caller can inspect the chosen path.
        print('Unable to complete manual design; check inputs and output destination.', file=sys.stderr)
        return 2
    finally:
        if output_fd is not None:
            os.close(output_fd)


if __name__ == '__main__':
    raise SystemExit(main())
