"""TCP framing adapter for pinned pysolarmanv5 3.0.6."""
import queue
import selectors
from functools import lru_cache

class FrameStream:
    def __init__(self):self.buffer=bytearray()
    def feed(self,data):
        self.buffer.extend(data);frames=[]
        while self.buffer:
            start=self.buffer.find(b'\xa5')
            if start<0:self.buffer.clear();break
            if start:del self.buffer[:start]
            if len(self.buffer)<3:break
            size=13+int.from_bytes(self.buffer[1:3],'little')
            if size>4096 or size<13:del self.buffer[0];continue
            if len(self.buffer)<size:break
            frame=bytes(self.buffer[:size])
            if frame[-1]!=0x15 or frame[-2]!=(sum(frame[1:-2])&255):del self.buffer[0];continue
            del self.buffer[:size];frames.append(frame)
        return frames

class FramedReceiver:
    def _send_receive_v5_frame(self,frame):
        # Arm before sendall so a fast response cannot arrive before data is wanted.
        self._data_wanted.set()
        try:return super()._send_receive_v5_frame(frame)
        finally:self._data_wanted.clear()
    def _data_receiver(self):
        parser=FrameStream();sock=self.sock
        try:
            self._poll.register(sock.fileno(),selectors.EVENT_READ)
            while not self._reader_exit.is_set():
                for _ in self._poll.select(.5):
                    if self._reader_exit.is_set():return
                    data=sock.recv(4096)
                    if not data:
                        if self._data_wanted.is_set():
                            try:self._data_queue.put_nowait(b'')
                            except queue.Full:pass
                        return
                    for frame in parser.feed(data):
                        if frame[3:5] != b'\x10\x15':continue
                        if not self._received_frame_is_valid(frame):continue
                        if self._data_wanted.is_set():
                            try:self._data_queue.put_nowait(frame)
                            except queue.Full:pass
        except (OSError,ValueError):
            if not self._reader_exit.is_set() and self._data_wanted.is_set():
                try:self._data_queue.put_nowait(b'')
                except queue.Full:pass

@lru_cache(maxsize=4)
def framed_class(base):return type('FramedSolarmanV5',(FramedReceiver,base),{})
def create_v5_client(*args,**kwargs):
    from pysolarmanv5 import PySolarmanV5
    return framed_class(PySolarmanV5)(*args,**kwargs)
