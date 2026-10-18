"""Isolated optional-runtime entrypoint. No Raven or user-plugin imports.

Only three Preview1 calls are provided. No WASI context, preopens, ambient
environment, sockets, clocks or entropy source are instantiated.
"""
from __future__ import annotations

import json
import os
import resource
import struct
import sys


def main():
    limits = json.loads(sys.argv[3])
    for kind, requested in ((resource.RLIMIT_CPU, limits['wall_seconds']+1),
                            (resource.RLIMIT_FSIZE, 65536), (resource.RLIMIT_NOFILE, 32)):
        _, hard = resource.getrlimit(kind)
        target = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(kind, (target, target))
    import wasmtime as W
    module_bytes = os.read(int(sys.argv[1]), (1 << 20)+1)
    source = os.read(int(sys.argv[2]), (1 << 20)+1)
    if len(module_bytes) > 1 << 20 or len(source) > 1 << 20:
        raise ValueError('Runner input exceeds its independent byte bound')
    config = W.Config()
    config.consume_fuel = True
    config.wasm_threads = False
    config.wasm_memory64 = False
    config.wasm_multi_memory = False
    config.max_wasm_stack = 256 << 10
    config.memory_reservation = limits['memory_bytes']
    config.memory_reservation_for_growth = 0
    config.memory_guard_size = 65536
    config.parallel_compilation = False
    # Bound the whole runner (including compiler/host allocations), distinct
    # from the much smaller guest linear-memory limit. Never raise an inherited
    # worker address-space cap.
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = min(256 << 20, hard) if hard != resource.RLIM_INFINITY else 256 << 20
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    engine = W.Engine(config)
    store = W.Store(engine)
    store.set_limits(memory_size=limits['memory_bytes'], table_elements=1024, instances=1, tables=1, memories=1)
    store.set_fuel(limits['fuel'])
    module = W.Module(engine, module_bytes)
    signatures = {'fd_read': ([W.ValType.i32()]*4, [W.ValType.i32()]),
                  'fd_write': ([W.ValType.i32()]*4, [W.ValType.i32()]),
                  'proc_exit': ([W.ValType.i32()], [])}
    for item in module.imports:
        if item.module != 'wasi_snapshot_preview1' or item.name not in signatures or not isinstance(item.type, W.FuncType):
            raise ValueError('Unsupported host import: '+item.module+'.'+item.name)
        parameters, results = signatures[item.name]
        if item.type.params != parameters or item.type.results != results:
            raise ValueError('Incorrect WASI import signature')
    output, errors = bytearray(), bytearray()
    cursor = 0

    def bounds(memory, caller, pointer, size):
        if pointer < 0 or size < 0 or pointer > memory.data_len(caller)-size:
            raise W.Trap('WASI memory range exceeds the exported memory')

    def vectors(caller, pointer, count, result):
        memory = caller.get('memory')
        if not isinstance(memory, W.Memory) or not 0 <= count <= 1024:
            raise W.Trap('WASI needs one exported memory and bounded iovecs')
        bounds(memory, caller, pointer, count*8)
        bounds(memory, caller, result, 4)
        items = []
        total = 0
        for offset in range(count):
            start, length = struct.unpack('<II', memory.read(caller, pointer+offset*8, pointer+offset*8+8))
            bounds(memory, caller, start, length)
            total += length
            if total > 1 << 20:
                raise W.Trap('WASI host I/O exceeds its per-call byte bound')
            items.append((start, length))
        return memory, items

    def fd_read(caller, fd, pointer, count, result):
        nonlocal cursor
        if fd != 0:
            return 8  # EBADF: no filesystem handles exist.
        memory, items = vectors(caller, pointer, count, result)
        consumed = 0
        for start, length in items:
            chunk = source[cursor:cursor+length]
            memory.write(caller, chunk, start)
            cursor += len(chunk)
            consumed += len(chunk)
        memory.write(caller, struct.pack('<I', consumed), result)
        return 0

    def fd_write(caller, fd, pointer, count, result):
        if fd not in (1, 2):
            return 8
        memory, items = vectors(caller, pointer, count, result)
        total = sum(length for _, length in items)
        if len(output)+len(errors)+total > limits['output_bytes']:
            raise W.Trap('WASI output budget exhausted')
        target = output if fd == 1 else errors
        for start, length in items:
            target.extend(memory.read(caller, start, start+length))
        memory.write(caller, struct.pack('<I', total), result)
        return 0

    class Exit(Exception):
        def __init__(self, code):
            self.code = code

    def proc_exit(code):
        raise Exit(code)

    linker = W.Linker(engine)
    for name, callback in (('fd_read', fd_read), ('fd_write', fd_write), ('proc_exit', proc_exit)):
        parameters, results = signatures[name]
        linker.define_func('wasi_snapshot_preview1', name, W.FuncType(parameters, results), callback, access_caller=name != 'proc_exit')
    try:
        instance = linker.instantiate(store, module)
        start = instance.exports(store).get('_start')
        if not isinstance(start, W.Func) or start.type(store).params or start.type(store).results:
            raise ValueError('WASI analysis must export _start: () -> ()')
        start(store)
    except Exit as exc:
        if exc.code:
            raise ValueError('WASI adapter exited with status '+str(exc.code)) from None
    os.write(1, output)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        sys.stderr.write(str(exc)[:4096]+'\n')
        raise SystemExit(2)
