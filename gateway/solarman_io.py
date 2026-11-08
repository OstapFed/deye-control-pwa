"""Paced Solarman requests over one connection, with block reads that fall back to single registers."""
import time

def plan_blocks(addresses, max_span=32, max_gap=12):
    """Group sorted register addresses into contiguous read blocks.

    A block never spans more than ``max_span`` registers and never bridges a gap
    longer than ``max_gap`` unused registers, so a block costs at most a few
    extra words compared with reading every register separately.
    """
    blocks = []
    for address in sorted(set(int(a) for a in addresses)):
        if blocks and address - blocks[-1][1] <= max_gap + 1 and address - blocks[-1][0] < max_span:
            blocks[-1][1] = address
        else:
            blocks.append([address, address])
    return [(start, end - start + 1) for start, end in blocks]


class PacedReader:
    def __init__(self, factory, delay=.5, retries=3, on_retry=None):
        self.factory=factory;self.delay=delay;self.retries=retries;self.on_retry=on_retry
        self.client=None;self.finished=0
        # Block reads are tried first; a logger that rejects them is read register by register.
        self.blocks_disabled_until=0.0
    def connection(self):
        if self.client is None:self.client=self.factory()
        return self.client
    def close(self):
        client,self.client=self.client,None
        if client is not None:
            try:client.disconnect()
            except Exception:pass
    def _pace(self):
        pause=self.delay-(time.monotonic()-self.finished)
        if pause>0:time.sleep(pause)
    def read(self,address,count=1,function=3,retries=None):
        retries=self.retries if retries is None else max(1,int(retries))
        last=None
        for attempt in range(1,retries+1):
            self._pace()
            try:
                client=self.connection()
                reader=client.read_holding_registers if function==3 else client.read_input_registers
                values=reader(address,count)
                if len(values)!=count:raise OSError('Incomplete register response')
                return list(values)
            except Exception as error:
                last=error;self.close()
                description=type(error).__name__+': '+(str(error) or repr(error))
                if self.on_retry:self.on_retry(address,attempt,description)
                if attempt<retries:time.sleep(1.5*attempt)
            finally:self.finished=time.monotonic()
        raise OSError('Register '+str(address)+' failed after '+str(retries)+' attempts: '+type(last).__name__+': '+(str(last) or repr(last))) from last
    def read_many(self,addresses,function=3,max_span=32,max_gap=12,block_cooldown=900):
        """Read a set of addresses; returns {address: raw word}."""
        result={}
        use_blocks=time.monotonic()>=self.blocks_disabled_until
        for start,count in plan_blocks(addresses,max_span,max_gap):
            if count>1 and use_blocks:
                try:
                    # One attempt only: a failed block is cheaper to replace with single reads.
                    values=self.read(start,count,function,retries=1)
                    result.update({start+i:v for i,v in enumerate(values)})
                    continue
                except OSError:
                    use_blocks=False;self.blocks_disabled_until=time.monotonic()+block_cooldown
            wanted=[a for a in addresses if start<=a<start+count]
            for address in sorted(set(wanted)):
                result[address]=self.read(address,1,function)[0]
        return result
    def write(self,address,value):
        """Single FC06 write on the shared connection. Never retried automatically."""
        self._pace()
        try:
            actual=self.connection().write_holding_register(address,value)
        except Exception:
            self.close();raise
        finally:self.finished=time.monotonic()
        if actual!=value:raise OSError('Запис Solarman не підтверджено')
        return actual
    def write_many(self,address,values):
        """FC16 write of consecutive registers (inverter clock). Never retried automatically."""
        self._pace()
        try:self.connection().write_multiple_holding_registers(address,list(values))
        except Exception:
            self.close();raise
        finally:self.finished=time.monotonic()
