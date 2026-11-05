"""Read-only identification and profile setup for Deye SG04LP3 (LV three phase)."""
import argparse,json,os,sys,time
from pathlib import Path

def decode_serial(words):return b''.join(int(x).to_bytes(2,'big') for x in words).decode('ascii').strip('\0 ')
def signed16(v):return v-65536 if v>=32768 else v
def signed32(low,high):
 v=low|(high<<16)
 return v-(1<<32) if v>=(1<<31) else v

def identify_and_read(args, attempt):
 client=None; stage='connecting to logger'
 try:
  from pysolarmanv5 import PySolarmanV5
  print('Attempt '+str(attempt)+'/3: connecting to '+args.host+':8899',flush=True)
  client=PySolarmanV5(args.host,args.serial,port=8899,mb_slave_id=args.unit,socket_timeout=6,auto_reconnect=False,verbose=False)
  stage='reading identity registers 0-9'; print(stage,flush=True)
  info=client.read_holding_registers(0,10)
  if len(info)!=10:raise ValueError('Incomplete identity reply')
  if info[0] not in (5,0x0500):raise ValueError('Expected LV three-phase hybrid device type 5. No profile applied.')
  identity=decode_serial(info[3:8])
  if args.expected_inverter_serial and identity!=args.expected_inverter_serial:raise ValueError('Inverter serial does not match expected device.')
  print('Inverter serial: '+identity,flush=True)
  cache={}
  # Use the exact single-register read mode that succeeded on the installed logger.
  requests=[(x,1) for x in (552,587,588,590,625,653,659,672,673,690)]
  for start,count in requests:
   stage='reading registers '+str(start)+'-'+str(start+count-1); print(stage,flush=True)
   values=client.read_holding_registers(start,count)
   if len(values)!=count:raise OSError('Incomplete register reply')
   cache.update({start+i:v for i,v in enumerate(values)})
  net=signed32(cache[625],cache[690]);load=signed32(cache[653],cache[659])
  point=dict(soc=cache[588],pv_w=cache[672]+cache[673],load_w=load,grid_on=bool(cache[552]&4),grid_import_w=max(0,net),grid_export_w=max(0,-net),battery_w=-signed16(cache[590]),battery_voltage_v=cache[587]*.01)
  if not 0<=point['soc']<=100 or not 0<=point['pv_w']<=25000 or not 0<=load<=60000 or abs(net)>60000 or abs(point['battery_w'])>20000 or not 35<=point['battery_voltage_v']<=65:raise ValueError('Telemetry outside plausible SG04LP3 ranges. Profile not applied; verify firmware/register map.')
  return point
 except Exception as error:
  raise RuntimeError(type(error).__name__+' at '+stage+': '+(str(error) or repr(error))) from error
 finally:
  if client:client.disconnect()

def main():
 p=argparse.ArgumentParser(description='Deye SG04LP3 read-only setup. No inverter writes.')
 p.add_argument('--host',required=True);p.add_argument('--serial',required=True,type=int);p.add_argument('--unit',type=int,default=1);p.add_argument('--expected-inverter-serial');p.add_argument('--apply',action='store_true')
 args=p.parse_args()
 try:
  point=None; last_error=None
  for attempt in range(1,4):
   try:
    point=identify_and_read(args,attempt); break
   except Exception as error:
    last_error=error; print('READ ERROR: '+str(error),flush=True)
    if isinstance(error.__cause__,(ValueError,ImportError)):break
    if attempt<3:time.sleep(2)
  if point is None:raise last_error
  print(json.dumps(point,indent=2));print('Battery power: positive = charging; negative = discharging.')
  print('Compare these values with the inverter display. Direct solar consumption is not inferred.')
  if args.apply:
   root=Path(__file__).parent;data=Path(os.environ.get('DEYE_DATA',str(root/'data')));data.mkdir(parents=True,exist_ok=True)
   file=data/'config.json';c=json.loads(file.read_text(encoding='utf-8-sig')) if file.exists() else {}
   if file.exists(): (data/('config.backup-'+str(int(time.time()))+'.json')).write_bytes(file.read_bytes())
   registers=json.loads((root/'profiles/deye_sg04lp3_readonly.json').read_text())
   c.update(mode='solarman_v5',host=args.host,port=8899,unit=args.unit,logger_serial=args.serial,registers=registers,verified_profile=False,allow_writes=False)
   temp=data/'config.pending';temp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(file)
   print('Local read-only profile saved. Inverter settings unchanged. Writes remain DISABLED.')
  return 0
 except Exception as e:print('Setup failed: '+type(e).__name__+': '+(str(e) or repr(e)),flush=True);return 1
if __name__=='__main__':sys.exit(main())
