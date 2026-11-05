"""Paced, read-only Solarman requests with bounded per-register retries."""
import time

class PacedReader:
    def __init__(self, factory, delay=.5, retries=3, on_retry=None):
        self.factory=factory;self.delay=delay;self.retries=retries;self.on_retry=on_retry
        self.client=None;self.finished=0
    def connection(self):
        if self.client is None:self.client=self.factory()
        return self.client
    def close(self):
        client,self.client=self.client,None
        if client is not None:
            try:client.disconnect()
            except OSError:pass
    def read(self,address,count=1,function=3):
        last=None
        for attempt in range(1,self.retries+1):
            pause=self.delay-(time.monotonic()-self.finished)
            if pause>0:time.sleep(pause)
            try:
                client=self.connection()
                reader=client.read_holding_registers if function==3 else client.read_input_registers
                values=reader(address,count)
                if len(values)!=count:raise OSError('Incomplete register response')
                return values
            except Exception as error:
                last=error;self.close()
                description=type(error).__name__+': '+(str(error) or repr(error))
                if self.on_retry:self.on_retry(address,attempt,description)
                if attempt<self.retries:time.sleep(1.5*attempt)
            finally:self.finished=time.monotonic()
        raise OSError('Register '+str(address)+' failed after '+str(self.retries)+' attempts: '+type(last).__name__+': '+(str(last) or repr(last))) from last
