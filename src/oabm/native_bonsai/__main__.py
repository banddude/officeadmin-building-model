"""Explicit local runtime CLI; request JSON never contains runtime paths or code."""
import argparse
from pathlib import Path
from . import NativeRuntime, export_native, probe_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', type=Path, required=True)
    parser.add_argument('--bonsai-python', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--model', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--timeout-seconds', type=float, default=600)
    args = parser.parse_args()
    runtime = NativeRuntime(args.blender, args.bonsai_python)
    if args.probe:
        if args.model or args.request:
            parser.error('--probe does not consume model/request')
        result = probe_runtime(runtime, args.output, args.timeout_seconds)
    else:
        if not args.model or not args.request:
            parser.error('--model and --request required without --probe')
        result = export_native(args.model, args.request, args.output, runtime, args.timeout_seconds)
    print(result)


if __name__ == '__main__':
    main()
