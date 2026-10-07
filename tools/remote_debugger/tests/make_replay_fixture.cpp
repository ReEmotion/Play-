#include "FrameDump.h"
#include "StdStream.h"
#include <cstring>
#include <cstdio>

int main(int argc, char** argv)
{
	if(argc != 2) return 1;
	CFrameDump dump;
	CGsPacketMetadata metadata(1);
	metadata.vu1State.nPC = 0;
	metadata.vu1State.nDelayedJumpAddr = MIPS_INVALID_PC;
	metadata.vu1State.nCOP2[0].nV3 = 0x3F800000;
	// IADDIU VI1, VI0, 7; NOP[E]; NOP. Shared assembler encodings from VuTest.
	const uint32 program[] = {0x10010007, 0x000002FF, 0x8000033C, 0x400002FF, 0x8000033C, 0x000002FF};
	memcpy(metadata.microMem1, program, sizeof(program));
	metadata.vuMemPacketAddress = 0;
	// Empty GIFtag: no payload required for deterministic packet inspection.
	const CGSHandler::RegisterWrite write(GS_REG_PRIM, CGSHandler::PRIM_SPRITE);
	if(!dump.AddRegisterPacket(&write, 1, &metadata)) return 1;
	Framework::CStdStream output(argv[1], "wb"); dump.Write(output);
	return 0;
}
