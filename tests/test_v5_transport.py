import sys,struct,unittest,socket,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from solarman_client import FrameStream,create_v5_client

def crc(data):
    v=0xffff
    for b in data:
        v^=b
        for _ in range(8):v=(v>>1)^0xa001 if v&1 else v>>1
    return struct.pack('<H',v)
def packet(sequence=1,serial=123,control=0x1510,rtu=None):
    payload=b'\x02'+bytes(13)+(rtu if rtu is not None else b'\x01\x03\x02\x00\x50'+crc(b'\x01\x03\x02\x00\x50'))
    frame=bytearray(struct.pack('<BHHHI',0xa5,len(payload),control,sequence,serial)+payload+b'\x00\x15')
    frame[-2]=sum(frame[1:-2])&255
    return bytes(frame)
class ParserTests(unittest.TestCase):
    def test_fragmentation(self):
        p=FrameStream();frame=packet();out=[]
        for byte in frame:out+=p.feed(bytes([byte]))
        self.assertEqual(out,[frame])
    def test_coalescing_and_noise(self):
        a=packet(control=0x4710);b=packet();self.assertEqual(FrameStream().feed(b'noise'+a+b),[a,b])
    def test_invalid_checksum_skipped(self):
        broken=bytearray(packet());broken[-2]^=1
        self.assertEqual(FrameStream().feed(bytes(broken)+packet()),[packet()])
try:
    import pysolarmanv5
    HAS_LIBRARY=True
except ImportError:HAS_LIBRARY=False
@unittest.skipUnless(HAS_LIBRARY,'Install gateway/requirements.txt for real-library TCP tests')
class WireTests(unittest.TestCase):
    def exercise(self,mode):
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen();server.settimeout(5)
        port=server.getsockname()[1];errors=[]
        def exact(c,n):
            data=b''
            while len(data)<n:
                b=c.recv(n-len(data))
                if not b:raise OSError('closed')
                data+=b
            return data
        def peer():
            try:
                c,_=server.accept()
                with c:
                    c.settimeout(5)
                    for _ in range(3):
                        header=exact(c,11);length=int.from_bytes(header[1:3],'little');rest=exact(c,length+2)
                        req=(header+rest)[26:-2];unit,fc=req[:2]
                        response=bytes([unit,fc,2])+struct.pack('>H',80);response+=crc(response)
                        seq=int.from_bytes(header[5:7],'little');sn=int.from_bytes(header[7:11],'little');frame=packet(seq,sn,rtu=response)
                        if mode=='fragments':
                            for part in (frame[:2],frame[2:5],frame[5:13],frame[13:]):c.sendall(part);time.sleep(.01)
                        elif mode=='counter':c.sendall(packet(seq,sn,control=0x4710)+frame)
                        else:c.sendall(frame)
            except Exception as e:errors.append(e)
            finally:server.close()
        t=threading.Thread(target=peer);t.start();client=None
        try:
            client=create_v5_client('127.0.0.1',123,port=port,socket_timeout=1,auto_reconnect=False)
            for _ in range(3):self.assertEqual(client.read_holding_registers(588,1),[80])
        finally:
            if client:client.disconnect()
            t.join(5)
        self.assertFalse(errors,errors)
    def test_fragmented_tcp_reply(self):self.exercise('fragments')
    def test_counter_and_reply_in_same_tcp_read(self):self.exercise('counter')
    def test_fast_replies(self):self.exercise('fast')
if __name__=='__main__':unittest.main()
