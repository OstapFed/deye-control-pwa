"""Read-only identification and profile setup for Deye hybrids (SG04LP3 three-phase, SG0xLP1 single-phase)."""
import argparse,json,os,sys,time
from pathlib import Path
from solarman_io import PacedReader
from solarman_client import create_v5_client
from config_storage import read_config

def decode_serial(words):return b''.join(int(x).to_bytes(2,'big') for x in words).decode('ascii').strip('\0 ')
def signed16(v):return v-65536 if v>=32768 else v
def signed32(low,high):
 v=low|(high<<16)
 return v-(1<<32) if v>=(1<<31) else v

# Deye device type (holding register 0) -> profile. 5: LV three-phase (SG04LP3), 3: single-phase hybrid (SG0xLP1).
PROFILES={5:('deye_sg04lp3','Deye SG04LP3 (LV three-phase hybrid)'),3:('deye_sg0xlp1','Deye SG0xLP1 (single-phase hybrid)')}
CHECK=('soc','pv_w','load_w','grid_on','grid_import_w','grid_export_w','battery_w','battery_voltage_v')

def decode(r,raw):
 v=raw[int(r['address'])];bits=16
 if 'address_high' in r:v|=raw[int(r['address_high'])]<<16;bits=32
 if r.get('signed') and v>=1<<(bits-1):v-=1<<bits
 if 'bit' in r:v=(v>>int(r['bit']))&1
 for a in r.get('sum_addresses',[]):v+=raw[int(a)]
 v=(v-float(r.get('offset',0)))*float(r.get('scale',1))
 if r.get('clip')=='positive':v=max(0,v)
 if r.get('clip')=='negative':v=max(0,-v)
 return round(v,3)

def identify_and_read(args, attempt):
 client=None; paced=None; stage='connecting to logger'
 try:
  import pysolarmanv5
  print('Attempt '+str(attempt)+'/3: connecting to '+args.host+':8899',flush=True)
  factory=lambda: create_v5_client(args.host,args.serial,port=8899,mb_slave_id=args.unit,socket_timeout=6,auto_reconnect=False,verbose=False)
  paced=PacedReader(factory,on_retry=lambda address,attempt,reason:print('Register '+str(address)+' attempt '+str(attempt)+'/3: '+reason,flush=True))
  stage='reading identity registers 0-9'; print(stage,flush=True)
  info=paced.read(0,10)
  if len(info)!=10:raise ValueError('Incomplete identity reply')
  kind=info[0]>>8 if info[0]>255 else info[0]
  if kind not in PROFILES:raise ValueError('Unsupported Deye device type '+str(info[0])+'. No profile applied.')
  name,title=PROFILES[kind];print('Inverter type: '+title,flush=True)
  identity=decode_serial(info[3:8])
  if args.expected_inverter_serial and identity!=args.expected_inverter_serial:raise ValueError('Inverter serial does not match expected device.')
  print('Inverter serial: '+identity,flush=True)
  profile=json.loads((Path(__file__).parent/'profiles'/(name+'.json')).read_text(encoding='utf-8'))
  channels={k:profile[k] for k in CHECK if k in profile}
  addresses=sorted({int(a) for r in channels.values() for a in [r['address'],*r.get('sum_addresses',[])]+([r['address_high']] if 'address_high' in r else [])})
  raw={}
  for address in addresses:
   stage='reading register '+str(address); print(stage,flush=True)
   raw[address]=paced.read(address,1)[0]
  point={k:decode(r,raw) for k,r in channels.items()};point['grid_on']=bool(point['grid_on'])
  if not 0<=point['soc']<=100 or not 0<=point['pv_w']<=30000 or abs(point['load_w'])>60000 or abs(point['battery_w'])>25000 or not 35<=point.get('battery_voltage_v',48)<=65:raise ValueError('Telemetry outside plausible ranges for '+title+'. Profile not applied; verify firmware/register map.')
  return point,name
 except Exception as error:
  raise RuntimeError(type(error).__name__+' at '+stage+': '+(str(error) or repr(error))) from error
 finally:
  if paced:paced.close()

def main():
 p=argparse.ArgumentParser(description='Deye hybrid read-only setup. No inverter writes.')
 p.add_argument('--host',required=True);p.add_argument('--serial',required=True,type=int);p.add_argument('--unit',type=int,default=1);p.add_argument('--expected-inverter-serial');p.add_argument('--apply',action='store_true')
 args=p.parse_args()
 print('DEYE BUILD: 2026.11.09-multi-model-5 (auto-detects three-phase / single-phase)',flush=True)
 try:
  point=None; last_error=None
  for attempt in range(1,4):
   try:
    point,profile_name=identify_and_read(args,attempt); break
   except Exception as error:
    last_error=error; print('READ ERROR: '+str(error),flush=True)
    if isinstance(error.__cause__,(ValueError,ImportError)):break
    if attempt<3:time.sleep(2)
  if point is None:raise last_error
  print(json.dumps(point,indent=2));print('Battery power: positive = charging; negative = discharging.')
  print('Compare these values with the inverter display. Direct solar consumption is not inferred.')
  if args.apply:
   root=Path(__file__).parent;data=Path(os.environ.get('DEYE_DATA',str(root/'data')));data.mkdir(parents=True,exist_ok=True)
   file=data/'config.json';c=read_config(file) if file.exists() else {}
   if file.exists(): (data/('config.backup-'+str(int(time.time()))+'.json')).write_bytes(file.read_bytes())
   registers=json.loads((root/'profiles'/(profile_name+'.json')).read_text(encoding='utf-8'))
   # Write permission survives an update only if the register map and the device are exactly the same.
   same=c.get('registers')==registers and c.get('host')==args.host and int(c.get('logger_serial') or 0)==args.serial
   keep=dict(verified_profile=bool(c.get('verified_profile')),allow_writes=bool(c.get('allow_writes'))) if same else dict(verified_profile=False,allow_writes=False)
   c.update(profile_name=profile_name,mode='solarman_v5',host=args.host,port=8899,unit=args.unit,logger_serial=args.serial,registers=registers,poll_seconds=c.get('poll_seconds',10),**keep)
   temp=data/'config.pending';temp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(file)
   print('Local profile saved. Inverter settings unchanged. Writes '+('remain ENABLED (same register map).' if c['allow_writes'] else 'are DISABLED until enabled in the panel.'))
  return 0
 except Exception as e:print('Setup failed: '+type(e).__name__+': '+(str(e) or repr(e)),flush=True);return 1
if __name__=='__main__':sys.exit(main())
