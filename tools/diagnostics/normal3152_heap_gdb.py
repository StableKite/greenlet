"""GDB-side read-only glibc 2.39 arena/tcache snapshots at an inert C marker.

No inferior calls, writes, allocator settings or Python object casts are used.
The stopped inferior's matching libc debug types describe every structure.
Safe-link decoding follows glibc-2.39 malloc.c PROTECT_PTR/REVEAL_PTR.
"""
from pathlib import Path
import json
import os
import traceback
import gdb

OUT = Path(os.environ['SK_HEAP_GDB_OUT'])
OUT.mkdir()


def address(value):
    return int(value)


def pointer(value, typename):
    return gdb.Value(value).cast(gdb.lookup_type(typename).pointer())


def chunk(value):
    p = pointer(value, 'struct malloc_chunk').dereference()
    raw = int(p['mchunk_size'])
    return p, {'address': hex(value), 'raw_size': raw, 'bytes': raw & ~7,
               'prev_inuse': bool(raw & 1), 'is_mmapped': bool(raw & 2),
               'non_main_arena': bool(raw & 4), 'prev_size': int(p['mchunk_prev_size'])}


def snapshot(phase):
    inferior = gdb.selected_inferior()
    pid = inferior.pid
    result = {'phase': phase, 'pid': pid, 'thread': gdb.selected_thread().ptid,
              'scope': 'READ_ONLY_GLIBC_STRUCTURES_NO_INFERIOR_CALLS_OR_WRITES',
              'arenas': [], 'tcache': []}
    for name in ('maps', 'smaps'):
        (OUT / ('phase-' + str(phase) + '.' + name)).write_bytes(Path('/proc/' + str(pid) + '/' + name).read_bytes())
    try:
        size = gdb.lookup_type('size_t').sizeof
        assert size == 8
        tcache = gdb.parse_and_eval('tcache')
        result['main_thread_tcache'] = hex(address(tcache))
        cached = set()
        if tcache:
            bins = int(gdb.parse_and_eval('mp_.tcache_bins'))
            assert 0 < bins <= 128
            for index in range(bins):
                count = int(tcache['counts'][index])
                entry = address(tcache['entries'][index])
                assert 0 <= count <= 64
                seen = set()
                for ordinal in range(count):
                    assert entry and entry not in seen
                    seen.add(entry)
                    cached.add(entry - 2 * size)
                    raw = bytes(inferior.read_memory(entry, size))
                    next_entry = int.from_bytes(raw, 'little') ^ (entry >> 12)
                    _, item = chunk(entry - 2 * size)
                    item.update(bin=index, ordinal=ordinal, user=hex(entry), safe_link_next=hex(next_entry))
                    result['tcache'].append(item)
                    entry = next_entry
                assert entry == 0
        chunk_type = gdb.lookup_type('struct malloc_chunk')
        fd_offset = next(field.bitpos // 8 for field in chunk_type.fields() if field.name == 'fd')
        main = gdb.parse_and_eval('&main_arena')
        arena_ptr = main
        seen_arenas = set()
        while address(arena_ptr) not in seen_arenas:
            assert len(seen_arenas) < 128
            arena_address = address(arena_ptr)
            seen_arenas.add(arena_address)
            state = arena_ptr.dereference()
            record = {'address': hex(arena_address), 'main': arena_address == address(main),
                      'attached_threads': int(state['attached_threads']),
                      'system_mem': int(state['system_mem']), 'max_system_mem': int(state['max_system_mem']),
                      'free_bins': [], 'fast_bins': []}
            result['arenas'].append(record)
            _, record['top'] = chunk(address(state['top']))
            for index in range(127):
                bin_pointer = state['bins'][2 * index]
                sentinel = address(state['bins'][2 * index].address) - fd_offset
                current = address(bin_pointer)
                visited = set()
                while current != sentinel:
                    assert current and current not in visited and len(visited) < 65536
                    visited.add(current)
                    value, item = chunk(current)
                    item['bin'] = index + 1
                    if item['bytes'] >= 1024 * 1024:
                        _, adjacent = chunk(current + item['bytes'])
                        adjacent['in_main_tcache'] = current + item['bytes'] in cached
                        adjacent['user'] = hex(current + item['bytes'] + 2 * size)
                        item['next_chunk'] = adjacent
                    record['free_bins'].append(item)
                    current = address(value['fd'])
            fast_length = int(state['fastbinsY'].type.range()[1]) + 1
            for index in range(fast_length):
                current = address(state['fastbinsY'][index])
                visited = set()
                while current:
                    assert current not in visited and len(visited) < 65536
                    visited.add(current)
                    value, item = chunk(current)
                    item['bin'] = index
                    record['fast_bins'].append(item)
                    current = address(value['fd']) ^ ((current + fd_offset) >> 12)
            arena_ptr = state['next']
        result['status'] = 'PASS_READ_ONLY_ARENA_AND_MAIN_TCACHE_SNAPSHOT'
    except Exception:
        result['status'] = 'DIAGNOSTIC_SNAPSHOT_ERROR_NO_ACCEPTANCE_CLAIM'
        result['error'] = traceback.format_exc()
    (OUT / ('phase-' + str(phase) + '.json')).write_text(json.dumps(result, indent=2) + '\n')
    gdb.write('SK_HEAP_SNAPSHOT ' + str(phase) + ' ' + result['status'] + '\n')


class SnapshotBreakpoint(gdb.Breakpoint):
    def stop(self):
        snapshot(int(gdb.parse_and_eval('phase')))
        return False


SnapshotBreakpoint('sk_malloc_snapshot_marker')
