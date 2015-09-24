from migen.fhdl.std import *
from migen.bus.transactions import *
from migen.bank.description import CSRStorage
from migen.genlib.record import *
from migen.genlib.misc import chooser

from misoc.interconnect import csr


_layout = [
    ("adr",  "address_width", DIR_M_TO_S),
    ("we",                 1, DIR_M_TO_S),
    ("dat_w",   "data_width", DIR_M_TO_S),
    ("dat_r",   "data_width", DIR_S_TO_M)
]


class Interface(Record):
    def __init__(self, data_width=8, address_width=14):
        Record.__init__(self, set_layout_parameters(_layout,
            data_width=data_width, address_width=address_width))


class Interconnect(Module):
    def __init__(self, master, slaves):
        self.comb += master.connect(*slaves)


class Initiator(Module):
    def __init__(self, generator, bus=None):
        self.generator = generator
        if bus is None:
            bus = Interface()
        self.bus = bus
        self.transaction = None
        self.read_data_ready = False
        self.done = False

    def do_simulation(self, selfp):
        if not self.done:
            if self.transaction is not None:
                if isinstance(self.transaction, TRead):
                    if self.read_data_ready:
                        self.transaction.data = selfp.bus.dat_r
                        self.transaction = None
                        self.read_data_ready = False
                    else:
                        self.read_data_ready = True
                else:
                    selfp.bus.we = 0
                    self.transaction = None
            if self.transaction is None:
                try:
                    self.transaction = next(self.generator)
                except StopIteration:
                    self.transaction = None
                    raise StopSimulation
                if self.transaction is not None:
                    selfp.bus.adr = self.transaction.address
                    if isinstance(self.transaction, TWrite):
                        selfp.bus.we = 1
                        selfp.bus.dat_w = self.transaction.data


class SRAM(Module):
    def __init__(self, mem_or_size, address, read_only=None, init=None, bus=None):
        if bus is None:
            bus = Interface()
        self.bus = bus
        data_width = flen(self.bus.dat_w)
        if isinstance(mem_or_size, Memory):
            mem = mem_or_size
        else:
            mem = Memory(data_width, mem_or_size//(data_width//8), init=init)
        csrw_per_memw = (mem.width + data_width - 1)//data_width
        word_bits = log2_int(csrw_per_memw)
        page_bits = log2_int((mem.depth*csrw_per_memw + 511)//512, False)
        if page_bits:
            self._page = CSRStorage(page_bits, name=mem.name_override + "_page")
        else:
            self._page = None
        if read_only is None:
            if hasattr(mem, "bus_read_only"):
                read_only = mem.bus_read_only
            else:
                read_only = False

        ###

        port = mem.get_port(write_capable=not read_only)
        self.specials += mem, port

        sel = Signal()
        sel_r = Signal()
        self.sync += sel_r.eq(sel)
        self.comb += sel.eq(self.bus.adr[9:] == address)

        if word_bits:
            word_index = Signal(word_bits)
            word_expanded = Signal(csrw_per_memw*data_width)
            self.sync += word_index.eq(self.bus.adr[:word_bits])
            self.comb += [
                word_expanded.eq(port.dat_r),
                If(sel_r,
                    chooser(word_expanded, word_index, self.bus.dat_r, n=csrw_per_memw, reverse=True)
                )
            ]
            if not read_only:
                wregs = []
                for i in range(csrw_per_memw-1):
                    wreg = Signal(data_width)
                    self.sync += If(sel & self.bus.we & (self.bus.adr[:word_bits] == i), wreg.eq(self.bus.dat_w))
                    wregs.append(wreg)
                memword_chunks = [self.bus.dat_w] + list(reversed(wregs))
                self.comb += [
                    port.we.eq(sel & self.bus.we & (self.bus.adr[:word_bits] == csrw_per_memw - 1)),
                    port.dat_w.eq(Cat(*memword_chunks))
                ]
        else:
            self.comb += If(sel_r, self.bus.dat_r.eq(port.dat_r))
            if not read_only:
                self.comb += [
                    port.we.eq(sel & self.bus.we),
                    port.dat_w.eq(self.bus.dat_w)
                ]

        if self._page is None:
            self.comb += port.adr.eq(self.bus.adr[word_bits:word_bits+flen(port.adr)])
        else:
            pv = self._page.storage
            self.comb += port.adr.eq(Cat(self.bus.adr[word_bits:word_bits+flen(port.adr)-flen(pv)], pv))

    def get_csrs(self):
        if self._page is None:
            return []
        else:
            return [self._page]


class CSRBank(csr.GenericBank):
    def __init__(self, description, address=0, bus=None):
        if bus is None:
            bus = Interface()
        self.bus = bus

        ###

        GenericBank.__init__(self, description, flen(self.bus.dat_w))

        sel = Signal()
        self.comb += sel.eq(self.bus.adr[9:] == address)

        for i, c in enumerate(self.simple_csrs):
            self.comb += [
                c.r.eq(self.bus.dat_w[:c.size]),
                c.re.eq(sel & \
                    self.bus.we & \
                    (self.bus.adr[:self.decode_bits] == i))
            ]

        brcases = dict((i, self.bus.dat_r.eq(c.w)) for i, c in enumerate(self.simple_csrs))
        self.sync += [
            self.bus.dat_r.eq(0),
            If(sel, Case(self.bus.adr[:self.decode_bits], brcases))
        ]


# address_map(name, memory) returns the CSR offset at which to map
# the CSR object (register bank or memory).
# If memory=None, the object is the register bank of object source.name.
# Otherwise, it is a memory object belonging to source.name.
# address_map is called exactly once for each object at each call to
# scan(), so it can have side effects.
class CSRBankArray(Module):
    def __init__(self, source, address_map, *ifargs, **ifkwargs):
        self.source = source
        self.address_map = address_map
        self.scan(ifargs, ifkwargs)

    def scan(self, ifargs, ifkwargs):
        self.banks = []
        self.srams = []
        for name, obj in xdir(self.source, True):
            if hasattr(obj, "get_csrs"):
                csrs = obj.get_csrs()
            else:
                csrs = []
            if hasattr(obj, "get_memories"):
                memories = obj.get_memories()
                for memory in memories:
                    mapaddr = self.address_map(name, memory)
                    if mapaddr is None:
                        continue
                    sram_bus = csr.Interface(*ifargs, **ifkwargs)
                    mmap = csr.SRAM(memory, mapaddr, bus=sram_bus)
                    self.submodules += mmap
                    csrs += mmap.get_csrs()
                    self.srams.append((name, memory, mapaddr, mmap))
            if csrs:
                mapaddr = self.address_map(name, None)
                if mapaddr is None:
                    continue
                bank_bus = csr.Interface(*ifargs, **ifkwargs)
                rmap = Bank(csrs, mapaddr, bus=bank_bus)
                self.submodules += rmap
                self.banks.append((name, csrs, mapaddr, rmap))

    def get_rmaps(self):
        return [rmap for name, csrs, mapaddr, rmap in self.banks]

    def get_mmaps(self):
        return [mmap for name, memory, mapaddr, mmap in self.srams]

    def get_buses(self):
        return [i.bus for i in self.get_rmaps() + self.get_mmaps()]
