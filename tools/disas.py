"""Disassemble functions from the shared cache by symbol name: disas.py <bytes> <symbol>..."""
import os
import struct
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'prototype'))
from sym import Symbols
from capstone import *
path=os.environ.get('PP_CACHE', '/tmp/fw/root/BrightonMaps10B350.N42OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s')
data=open(path,'rb').read(); s=Symbols(); s.add_cache(data); s.finish()
inv={}
for a,n in zip(s.addrs,s.names): inv.setdefault(n.split(' (')[0],a)
mo,mc=struct.unpack_from('<II',data,0x10)
maps=[struct.unpack_from('<QQQII',data,mo+32*i) for i in range(mc)]
def fo(a):
    for addr,size,off,_,_ in maps:
        if addr<=a<addr+size: return off+a-addr
for name in sys.argv[2:]:
    a=inv[name]; thumb=a&1; a&=~1
    md=Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
    print('==',name,hex(a),'thumb' if thumb else 'arm')
    for i in list(md.disasm(data[fo(a):fo(a)+int(sys.argv[1])],a)):
        print(f'  {i.address:#x} {i.mnemonic} {i.op_str}')
