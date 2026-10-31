"""Static Mach-O reader extracted from the verified research implementation."""
from pathlib import Path
import struct

class MachO:
    def __init__(self, path):
        self.path = Path(path)
        self.blob = self.path.read_bytes()
        if struct.unpack_from('<I', self.blob)[0] != 0xFEEDFACF:
            raise ValueError('Expected a thin little-endian Mach-O 64-bit binary')
        self.sections = []
        self.segments = []
        off = 32
        for _ in range(struct.unpack_from('<I', self.blob, 16)[0]):
            cmd, size = struct.unpack_from('<II', self.blob, off)
            if cmd == 0x19:
                vm, vmsize, fileoff, filesize = struct.unpack_from('<4Q', self.blob, off + 24)
                self.segments.append((vm, vmsize, fileoff, filesize))
                count = struct.unpack_from('<I', self.blob, off + 64)[0]
                for n in range(count):
                    s = off + 72 + n * 80
                    name = self.blob[s:s+16].split(b'\0')[0].decode()
                    addr, length = struct.unpack_from('<QQ', self.blob, s+32)
                    self.sections.append((name, addr, length))
            off += size

    def offset(self, address):
        for vm, _, fileoff, filesize in self.segments:
            if vm <= address < vm + filesize:
                return fileoff + address - vm
        raise ValueError(f'Unmapped address {address:x}')

    def read(self, address, size):
        off = self.offset(address)
        return self.blob[off:off+size]

    def pointer(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def cstring(self, address):
        return self.read(address, 1024).split(b'\0', 1)[0].decode('utf-8', errors='replace')

    def section(self, address):
        return next((name for name, start, size in self.sections if start <= address < start+size), '')

    def describe(self, address):
        try:
            section = self.section(address)
            if section in ('__cstring', '__objc_methname', '__objc_classname'):
                return self.cstring(address)
            if section == '__cfstring':
                _, flags, ptr, length = struct.unpack('<4Q', self.read(address, 32))
                if length > 1000:
                    return ''
                return self.read(ptr, length * (2 if flags & 0x10 else 1)).decode(
                    'utf-16-le' if flags & 0x10 else 'utf-8', errors='replace')
            if section == '__objc_selrefs':
                return self.cstring(self.pointer(address))
            if section in ('__objc_classrefs', '__objc_superrefs'):
                ptr = self.pointer(address)
                ro = self.pointer(ptr + 32) & ~7
                return 'class ' + self.cstring(self.pointer(ro + 24))
            if section == '__objc_stubs':
                words = struct.unpack('<3I', self.read(address, 12))
                adrp, ldr = words[:2]
                if adrp & 0x9F00001F == 0x90000001 and ldr & 0xFFC003FF == 0xF9400021:
                    imm = ((adrp >> 29) & 3) | (((adrp >> 5) & 0x7FFFF) << 2)
                    if imm & (1 << 20):
                        imm -= 1 << 21
                    ref = (address & ~4095) + (imm << 12) + ((ldr >> 10) & 4095) * 8
                    return 'objc ' + self.cstring(self.pointer(ref))
        except (ValueError, struct.error):
            pass
        return ''
