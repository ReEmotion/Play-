"""Generate a tiny redistributable PS2 ELF with an EE counter and GS sprite.

No BIOS, assembler or PS2 SDK is required. Emits ELF32/MIPS at 0x00100000.
"""
import struct
import sys
from pathlib import Path


def make_elf(path):
    base = 0x100000
    code = []
    def emit(op):
        code.append(op)
    def imm(reg, value):
        emit(0x3C000000 | reg << 16 | (value >> 16))  # lui
        emit(0x34000000 | reg << 21 | reg << 16 | (value & 0xffff))
    def store(reg, offset, address_reg=9):
        emit(0xAC000000 | address_reg << 21 | reg << 16 | offset)
    imm(3, 2); imm(4, 0); imm(5, 2); imm(6, 0)
    emit(0x0000000C)  # GsSetCrt(0, NTSC, 0)
    imm(9, 0x12000000)
    imm(8, 0xFF61); store(8, 0x00)
    imm(8, 0); store(8, 0x20)
    imm(8, 10 << 9); store(8, 0x70); store(0, 0x74); store(0, 0x80)
    imm(8, (639 | 447 << 12)); store(8, 0x84)
    # Base GS sprite A+D packet, repeatedly sent through GIF FIFO.
    writes = [(10 << 16, 0x4c), (1 << 32, 0x4e), (639 << 16 | 447 << 48, 0x40),
              (0, 0x47), (0, 0x18), (6, 0), (0x3F800000802060A0, 1),
              (0, 5), (640 * 16 | (448 * 16) << 16, 5)]
    packet = struct.pack('<QQ', len(writes) | (1 << 15) | (1 << 60), 0xe)
    packet += b''.join(struct.pack('<QQ', value, register) for value, register in writes)
    # Fixed data offset keeps addresses deterministic.
    imm(9, 0x10006000)
    imm(10, base + 0x400)
    imm(11, base + 0x600)
    imm(8, 0)
    loop = base + len(code) * 4
    for offset in range(0, len(packet), 16):
        emit(0x78000000 | 10 << 21 | 12 << 16 | offset)  # lq t4, offset(t2)
        emit(0x7C000000 | 9 << 21 | 12 << 16)  # sq t4, 0(t1)
    imm(13, 0x20000)
    delay = base + len(code) * 4
    emit(0x25ADFFFF)  # addiu t5,t5,-1
    emit(0x15A0FFFE)  # bne t5,r0,delay
    emit(0)
    emit(0x25080001)  # addiu t0,t0,1
    emit(0xAD680000)  # sw t0,0(t3)
    emit(0x08000000 | (loop >> 2))
    emit(0)
    payload = struct.pack('<' + 'I' * len(code), *code).ljust(0x400, b'\0') + packet
    payload = payload.ljust(0x604, b'\0')
    header = struct.pack('<16sHHIIIIIHHHHHH', b'\x7fELF\x01\x01\x01' + b'\0' * 9,
                         2, 8, 1, base, 52, 0, 0x20924001, 52, 32, 1, 40, 0, 0)
    program = struct.pack('<IIIIIIII', 1, 0x100, base, base, len(payload), len(payload), 7, 16)
    Path(path).write_bytes((header + program).ljust(0x100, b'\0') + payload)
    return {'entry': hex(base), 'loop': hex(loop), 'counter': hex(base + 0x600)}


if __name__ == '__main__':
    print(make_elf(sys.argv[1]))
