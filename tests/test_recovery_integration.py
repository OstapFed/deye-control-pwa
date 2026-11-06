"""Full process test: legacy Windows config -> V5 probe -> live polling.

Requires gateway/requirements.txt and local TCP sockets. No physical inverter.
"""
import base64,json,os,socketserver,struct,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
from solarman_client import FrameStream
try:
 import pysolarmanv5
 HAS_LIBRARY=True
except ImportError:HAS_LIBRARY=False

class Logger(socketserver.ThreadingTCPServer):
 allow_reuse_address=True
 daemon_threads=True

def crc(data):
 value=0xffff
 for byte in data:
  value^=byte
  for _ in range(8):value=(value>>1)^0xa001 if value&1 else value>>1
 return struct.pack('<H',value)

class Reply(socketserver.BaseRequestHandler):
 def handle(self):
  frames=FrameStream();self.request.settimeout(30)
  try:
   while True:
    data=self.request.recv(4096)
    if not data:return
    for frame in frames.feed(data):
     rtu=frame[26:-2];unit,fc,address,count=struct.unpack('>BBHH',rtu[:6])
     if fc!=3:raise AssertionError('Integration scenario must not write')
     values=self.server.values
     payload=bytes([unit,fc,count*2])+b''.join(struct.pack('>H',values.get(n,0)) for n in range(address,address+count))
     payload+=crc(payload)
     body=b'\x02'+bytes(13)+payload
     result=bytearray(b'\xa5'+struct.pack('<H',len(body))+b'\x10\x15'+frame[5:11]+body+b'\x00\x15')
     result[-2]=sum(result[1:-2])&255
     self.request.sendall(result[:4]);time.sleep(.002);self.request.sendall(result[4:])
  except (OSError,ConnectionError):pass

@unittest.skipUnless(HAS_LIBRARY,'Install gateway/requirements.txt for process integration test')
class RecoveryIntegration(unittest.TestCase):
 def test_legacy_config_then_two_fresh_samples(self):
  root=Path(__file__).parents[1];data=Path(tempfile.mkdtemp());logger=Logger(('127.0.0.1',0),Reply)
  logger.values={0:5,3:0x3132,4:0x3334,5:0x3536,6:0x3738,7:0x3930,552:4,587:5287,588:80,590:65529,625:756,653:633}
  threading.Thread(target=logger.serve_forever,daemon=True).start()
  old={'address':'Будинок у Києві','mode':'demo','allow_writes':False,'group':'2.1'}
  legacy=json.dumps(old,ensure_ascii=False).encode('cp1251');(data/'config.json').write_bytes(legacy)
  env=os.environ.copy();env.update(DEYE_DATA=str(data),DEYE_PASSWORD='integration-password-123',PYTHONIOENCODING='utf-8')
  process=None
  try:
   # Configure CLI normally uses 8899. Exercise its actual function with a test-port factory.
   runner="""import sys,importlib.util
from solarman_client import create_v5_client
spec=importlib.util.spec_from_file_location('setup',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
port=int(sys.argv[2]);original=create_v5_client
m.create_v5_client=lambda *a,**k: original(*a,**dict(k,port=port))
sys.argv=['configure','--host','127.0.0.1','--serial','123','--expected-inverter-serial','1234567890','--apply']
raise SystemExit(m.main())
"""
   completed=subprocess.run([sys.executable,'-c',runner,str(root/'gateway/configure_deye.py'),str(logger.server_address[1])],cwd=root/'gateway',env=env,capture_output=True,text=True,timeout=40)
   self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
   c=json.loads((data/'config.json').read_bytes().decode('utf-8'));self.assertEqual(c['address'],old['address']);self.assertFalse(c['allow_writes'])
   backups=list(data.glob('config.backup-*.json'));self.assertTrue(backups);self.assertEqual(backups[0].read_bytes(),legacy)
   c['port']=logger.server_address[1];c['poll_seconds']=6;(data/'config.json').write_text(json.dumps(c,ensure_ascii=False),encoding='utf-8')
   import socket
   with socket.socket() as reserve:reserve.bind(('127.0.0.1',0));http_port=reserve.getsockname()[1]
   env['DEYE_PORT']=str(http_port)
   process=subprocess.Popen([sys.executable,str(root/'gateway/app.py')],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   auth='Basic '+base64.b64encode(b'admin:integration-password-123').decode();timestamps=set();deadline=time.monotonic()+35
   while time.monotonic()<deadline:
    try:
     with urlopen(Request('http://127.0.0.1:'+str(http_port)+'/api/status',headers={'Authorization':auth}),timeout=2) as response:status=json.load(response)
     if status['online'] and status['sample'] and not status['sample']['demo']:
      point=status['sample'];self.assertEqual(point['soc'],80);self.assertEqual(point['grid_import_w'],756);timestamps.add(point['t'])
      if len(timestamps)>=2:break
    except OSError:pass
    time.sleep(.2)
   self.assertGreaterEqual(len(timestamps),2,'No two distinct live samples collected')
  finally:
   if process:
    process.terminate();process.communicate(timeout=5)
   logger.shutdown();logger.server_close()
if __name__=='__main__':unittest.main()
