import importlib.util,os,tempfile,unittest,json,time,sys
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
os.environ['DEYE_DATA']=tempfile.mkdtemp()
sys.path.insert(0,str(Path(__file__).parents[1]/'gateway'))
import solarman_io
spec=importlib.util.spec_from_file_location('gateway',Path(__file__).parents[1]/'gateway/app.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
class NoSleepTestCase(unittest.TestCase):
 def setUp(self):
  sleeper=patch('solarman_io.time.sleep');sleeper.start();self.addCleanup(sleeper.stop)
  app.reset_sessions();self.addCleanup(app.reset_sessions)

class GatewayTests(NoSleepTestCase):
 def setUp(self):
  app.save(dict(app.DEFAULT));app.db().close()
  with app.db() as d:d.execute('DELETE FROM samples');d.execute('DELETE FROM audit')
 def test_capacity_and_limits(self):
  c=app.DEFAULT|{'batteries':[{'name':'A','nominal_kwh':10,'usable_pct':70}]};self.assertEqual(app.validate(c)['capacity_kwh'],7)
  with self.assertRaises(ValueError):app.validate(c|{'reserve_soc':95,'target_soc':50})
 def test_unknown_energy_and_coverage(self):
  t=time.time();p=dict(pv_w=1000,load_w=500,demo=True)
  with app.db() as d:
   for ts in (t-60,t-30):d.execute('INSERT INTO samples VALUES(?,?)',(ts,json.dumps(p)))
  r=app.report();self.assertAlmostEqual(r['pv_kwh'],.0083,places=4);self.assertIsNone(r['grid_import_kwh']);self.assertEqual(r['metric_coverage_hours']['grid_import_kwh'],0)
 def test_report_separates_demo_and_real(self):
  t=time.time()
  with app.db() as d:
   for ts in (t-60,t-30):d.execute('INSERT INTO samples VALUES(?,?)',(ts,json.dumps(dict(pv_w=1000,load_w=500,demo=False))))
  self.assertEqual(app.report()['samples'],0)
 def test_bad_period(self):
  for v in ('nan','inf',-1,3000):
   with self.assertRaises(ValueError):app.report(v)
 def test_write_disabled(self):
  with self.assertRaises(ValueError):app.write_setting('target_soc',80,'test')
 def test_verified_write_and_readback(self):
  c=app.DEFAULT|{'mode':'modbus_tcp','verified_profile':True,'allow_writes':True,'registers':{'target_soc':{'address':100,'scale':1,'min':30,'max':100,'writable':True}}};app.save(c)
  with patch.object(app.Modbus,'read',side_effect=[60,80]),patch.object(app.Modbus,'request') as request:
   self.assertEqual(app.write_setting('target_soc',80,'test'),80);request.assert_called_once_with(6,100,80)
  with app.db() as d:self.assertEqual(d.execute('SELECT COUNT(*) FROM audit').fetchone()[0],2)
  with self.assertRaises(ValueError):app.write_setting('target_soc',101,'test')
 def test_readback_mismatch_fails(self):
  c=app.DEFAULT|{'mode':'modbus_tcp','verified_profile':True,'allow_writes':True,'registers':{'soc_limit':{'address':100,'min':0,'max':100,'writable':True}}};app.save(c)
  with patch.object(app.Modbus,'read',side_effect=[20,20]),patch.object(app.Modbus,'request'):
   with self.assertRaisesRegex(ValueError,'читання'):app.write_setting('soc_limit',30,'test')
if __name__=='__main__':unittest.main()

class HouseholdTests(NoSleepTestCase):
 def setUp(self):app.save(dict(app.DEFAULT));app.db().close()
 def test_site_added_locked_and_switch_preserves_profile(self):
  c=app.config();c['address']='Original';app.save(c)
  site=app.add_site({'host':'192.168.1.25','port':502,'name':'House 2'})
  app.select_site(site['site_id']);c=app.config()
  self.assertEqual(c['host'],'192.168.1.25');self.assertFalse(c['allow_writes']);self.assertFalse(c['verified_profile']);self.assertEqual(c['registers'],{})
  app.select_site('default');self.assertEqual(app.config()['address'],'Original')
 def test_public_and_large_scans_rejected(self):
  for cidr in ('8.8.8.0/24','192.168.0.0/16','127.0.0.0/24','::1/128'):
   with self.assertRaises(ValueError):app.discover_network(cidr)
 def test_scan_candidates_no_register_writes(self):
  from unittest.mock import MagicMock
  def probe(address,timeout):
   if address==('192.168.1.1',502):return MagicMock()
   raise OSError()
  with patch.object(app.socket,'create_connection',side_effect=probe):
   result=app.discover_network('192.168.1.0/30')
  self.assertEqual(len(result['devices']),1);self.assertTrue(result['devices'][0]['supported'])
 def test_battery_charge_discharge_channels(self):
  t=time.time()
  with app.db() as d:
   d.execute('DELETE FROM samples')
   for ts in (t-60,t-30):d.execute('INSERT INTO samples VALUES(?,?)',(ts,json.dumps(dict(pv_w=1000,load_w=500,battery_w=500,demo=True,site_id='default'))))
  r=app.report();self.assertAlmostEqual(r['battery_charge_kwh'],.0042,places=4);self.assertEqual(r['battery_discharge_kwh'],0)

class SolarmanTests(NoSleepTestCase):
 def test_v5_factory_and_read(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.read_holding_registers.return_value=[720]
  factory=MagicMock(return_value=client)
  c=app.DEFAULT|{'mode':'solarman_v5','host':'192.168.1.10','port':8899,'logger_serial':1234567890}
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):
   self.assertEqual(app.inverter(c).read({'address':100,'scale':.1}),72)
   self.assertEqual(app.inverter(c).read({'address':100,'scale':.1}),72)
  factory.assert_called_once_with('192.168.1.10',1234567890,port=8899,mb_slave_id=1,socket_timeout=5,auto_reconnect=False,verbose=False)
  client.disconnect.assert_not_called()
 def test_non_persistent_mode_disconnects(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.read_holding_registers.return_value=[720]
  c=app.DEFAULT|{'mode':'solarman_v5','logger_serial':123,'persistent_connection':False}
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}):
   app.inverter(c).read({'address':100})
  client.disconnect.assert_called_once()
 def test_disconnect_on_error(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.read_input_registers.side_effect=OSError('timeout')
  c=app.DEFAULT|{'mode':'solarman_v5','logger_serial':123}
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}):
   with self.assertRaises(OSError):app.inverter(c).read({'address':100,'function':4})
  self.assertEqual(client.disconnect.call_count,3)
 def test_write_echo_required(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.write_holding_register.return_value=10
  c=app.DEFAULT|{'mode':'solarman_v5','logger_serial':123}
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}):
   with self.assertRaises(OSError):app.inverter(c).request(6,100,20)
  client.write_holding_register.assert_called_once_with(100,20)

class CompositeProfileTests(NoSleepTestCase):
 def test_signed_grid_words_and_direction(self):
  m=app.Modbus(app.DEFAULT)
  with patch.object(m,'request',side_effect=[65036,65535]):self.assertEqual(m.read({'address':625,'address_high':690,'signed':True,'clip':'negative'}),500)
 def test_pv_sum_and_grid_relay(self):
  m=app.Modbus(app.DEFAULT)
  with patch.object(m,'request',side_effect=[1200,600]):self.assertEqual(m.read({'address':672,'sum_addresses':[673]}),1800)
  with patch.object(m,'request',return_value=5):self.assertEqual(m.read({'address':552,'bit':2}),1)
 def test_composite_write_rejected(self):
  with self.assertRaises(ValueError):app.validate(app.DEFAULT|{'registers':{'x':{'address':1,'bit':2,'writable':True,'min':0,'max':1}}})
 def test_serial_matches_user_photo(self):
  spec=importlib.util.spec_from_file_location('configure',Path(__file__).parents[1]/'gateway/configure_deye.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  self.assertEqual(m.decode_serial([12851,12593,13104,12848,14391]),'2311302087')
 def test_configure_applies_only_local_readonly_profile(self):
  import types,sys
  from unittest.mock import MagicMock
  spec=importlib.util.spec_from_file_location('configure',Path(__file__).parents[1]/'gateway/configure_deye.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  regs={0:5,3:0x3132,4:0x3334,5:0x3536,6:0x3738,7:0x3930,552:4,587:5000,588:72,590:65336,625:65036,690:65535,653:2000,659:0,672:1500,673:1200}
  client=MagicMock();client.read_holding_registers.side_effect=lambda a,n:[regs.get(i,0) for i in range(a,a+n)]
  dest=tempfile.mkdtemp()
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}),patch.dict(os.environ,{'DEYE_DATA':dest}),patch.object(sys,'argv',['configure','--host','192.168.1.10','--serial','123','--apply']),patch('builtins.print'):
   self.assertEqual(m.main(),0)
  c=json.loads((Path(dest)/'config.json').read_text());self.assertFalse(c['allow_writes']);self.assertFalse(c['verified_profile']);self.assertEqual(c['registers']['soc']['address'],588);client.write_holding_register.assert_not_called()

class HttpDisconnectTests(NoSleepTestCase):
 def test_browser_disconnects_do_not_escape(self):
  handler=object.__new__(app.Handler)
  for error in (ConnectionAbortedError(10053,'browser aborted'),ConnectionResetError('reset'),BrokenPipeError('closed')):
   with patch.object(app.BaseHTTPRequestHandler,'handle',side_effect=error):handler.handle()
 def test_other_errors_are_not_hidden(self):
  handler=object.__new__(app.Handler)
  with patch.object(app.BaseHTTPRequestHandler,'handle',side_effect=OSError('server error')):
   with self.assertRaises(OSError):handler.handle()

class BatchedSolarmanTests(NoSleepTestCase):
 def profile(self):
  profile=json.loads((Path(__file__).parents[1]/'gateway/profiles/deye_sg04lp3_readonly.json').read_text())
  return app.DEFAULT|{'mode':'solarman_v5','logger_serial':123,'registers':profile}
 def test_one_connection_per_sample_and_cached_directions(self):
  from unittest.mock import MagicMock
  import types,sys
  values={588:80,672:0,673:0,653:665,659:0,552:4,625:958,690:0,590:65365}
  client=MagicMock()
  def exact_words(start,count):
   if count!=1:raise TimeoutError('large block unsupported')
   return [values.get(start,0)]
  client.read_holding_registers.side_effect=exact_words
  factory=MagicMock(return_value=client)
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):p=app.read_sample(self.profile())
  self.assertEqual(p['soc'],80);self.assertEqual(p['load_w'],665);self.assertEqual(p['grid_import_w'],958);self.assertEqual(p['grid_export_w'],0);self.assertEqual(p['battery_w'],171)
  calls=client.read_holding_registers.call_args_list
  # One block attempt fails, then the logger is read register by register (9 words).
  self.assertEqual(sum(1 for x in calls if x.args[1]>1),1);self.assertEqual(sum(1 for x in calls if x.args[1]==1),9)
  client.read_holding_registers.reset_mock()
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):app.read_sample(self.profile())
  self.assertTrue(all(x.args[1]==1 for x in client.read_holding_registers.call_args_list),'blocks stay disabled after a failure')
 def test_failure_closes_session_and_next_cycle_reconnects(self):
  from unittest.mock import MagicMock
  import types,sys
  bad=MagicMock();bad.read_holding_registers.side_effect=TimeoutError('logger timeout')
  good=MagicMock();good.read_holding_registers.side_effect=lambda start,count:[50 if i==588 else 0 for i in range(start,start+count)]
  factory=MagicMock(side_effect=[bad,bad,bad,good])
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):
   with self.assertRaisesRegex(OSError,'Register 552 failed after 3 attempts'):app.read_sample(self.profile())
   self.assertEqual(app.read_sample(self.profile())['soc'],50)
  self.assertEqual(bad.disconnect.call_count,3);good.disconnect.assert_not_called()

class SetupRecoveryTests(NoSleepTestCase):
 def module(self):
  spec=importlib.util.spec_from_file_location('configure_retry',Path(__file__).parents[1]/'gateway/configure_deye.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_empty_timeout_has_type_and_stage(self):
  import types,sys
  from unittest.mock import MagicMock
  m=self.module();args=types.SimpleNamespace(host='192.168.1.10',serial=123,unit=1,expected_inverter_serial=None)
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(side_effect=TimeoutError()))}),patch('builtins.print'):
   with self.assertRaisesRegex(RuntimeError,'OSError at reading identity registers 0-9'):m.identify_and_read(args,1)
 def test_setup_retries_transient_error(self):
  import sys
  m=self.module()
  with patch.object(sys,'argv',['configure','--host','192.168.1.10','--serial','123']),patch.object(m,'identify_and_read',side_effect=[RuntimeError('TimeoutError at identity'),{'soc':80}]) as reader,patch.object(m.time,'sleep') as sleep,patch('builtins.print'):
   self.assertEqual(m.main(),0)
  self.assertEqual(reader.call_count,2);sleep.assert_called_once_with(2)

class PacedReadTests(NoSleepTestCase):
 def test_retry_only_failed_register(self):
  from unittest.mock import MagicMock,call
  from solarman_io import PacedReader
  first=MagicMock();first.read_holding_registers.side_effect=[[80],TimeoutError()]
  second=MagicMock();second.read_holding_registers.side_effect=[[756],[633]]
  factory=MagicMock(side_effect=[first,second]);reader=PacedReader(factory,delay=0)
  self.assertEqual(reader.read(588),[80]);self.assertEqual(reader.read(625),[756]);self.assertEqual(reader.read(653),[633]);reader.close()
  self.assertEqual(first.read_holding_registers.call_args_list,[call(588,1),call(625,1)])
  self.assertEqual(second.read_holding_registers.call_args_list,[call(625,1),call(653,1)])
  first.disconnect.assert_called_once();second.disconnect.assert_called_once();second.write_holding_register.assert_not_called()
 def test_pacing_is_applied(self):
  from unittest.mock import MagicMock
  import solarman_io
  reader=solarman_io.PacedReader(lambda:MagicMock(read_holding_registers=MagicMock(return_value=[80])),delay=.5)
  reader.finished=10
  with patch.object(solarman_io.time,'monotonic',return_value=10),patch.object(solarman_io.time,'sleep') as sleep:
   reader.read(588)
  sleep.assert_called_once_with(.5);reader.close()

class ConfigEncodingTests(NoSleepTestCase):
 def test_reads_utf8_and_bom_and_cp1251(self):
  from config_storage import read_config
  value={'address':'Будинок, Київ','group':'2.1','allow_writes':False}
  path=Path(tempfile.mkdtemp())/'config.json'
  for encoding in ('utf-8','utf-8-sig','cp1251'):
   path.write_bytes(json.dumps(value,ensure_ascii=False).encode(encoding))
   self.assertEqual(read_config(path),value)
 def test_save_migrates_without_losing_settings(self):
  path=app.DATA/'config.json';c=app.DEFAULT|{'address':'Будинок, Київ','group':'2.1','allow_writes':False}
  path.write_bytes(json.dumps(c,ensure_ascii=False).encode('cp1251'))
  loaded=app.config();self.assertEqual(loaded['address'],c['address']);app.save(loaded)
  self.assertEqual(json.loads(path.read_bytes().decode('utf-8'))['address'],c['address']);self.assertFalse(app.config()['allow_writes'])
 def test_invalid_json_is_not_overwritten(self):
  from config_storage import read_config
  path=Path(tempfile.mkdtemp())/'config.json';raw=b'{broken JSON';path.write_bytes(raw)
  with self.assertRaises(json.JSONDecodeError):read_config(path)
  self.assertEqual(path.read_bytes(),raw)

class LocalSessionTests(NoSleepTestCase):
 def handler(self,address,cookie='',authorization=''):
  h=object.__new__(app.Handler);h.client_address=(address,12345);h.headers={'Cookie':cookie,'Authorization':authorization};return h
 def test_local_cookie_accepts_stale_tab_password(self):
  h=self.handler('127.0.0.1','deye_local_session='+app.LOCAL_SESSION_TOKEN,'Basic wrong-old-password')
  self.assertTrue(h.auth())
 def test_cookie_not_accepted_from_remote_client(self):
  h=self.handler('192.168.1.50','deye_local_session='+app.LOCAL_SESSION_TOKEN)
  with patch.object(h,'send') as send:self.assertFalse(h.auth())
  self.assertEqual(send.call_args.args[0],401)
 def test_invalid_cookie_does_not_authenticate(self):
  h=self.handler('127.0.0.1','deye_local_session=wrong')
  with patch.object(h,'send') as send:self.assertFalse(h.auth())
  self.assertEqual(send.call_args.args[0],401)


class OptimisedPollingTests(NoSleepTestCase):
 def full(self):
  profile=json.loads((Path(__file__).parents[1]/'gateway/profiles/deye_sg04lp3.json').read_text())
  return app.DEFAULT|{'mode':'solarman_v5','logger_serial':123,'registers':profile}
 def client(self,values):
  from unittest.mock import MagicMock
  client=MagicMock();client.read_holding_registers.side_effect=lambda a,n:[values.get(i,0) for i in range(a,a+n)];return client
 def run_sample(self,client,c):
  import types,sys
  from unittest.mock import MagicMock
  factory=MagicMock(return_value=client)
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):return app.read_sample(c),factory
 def test_block_plan(self):
  from solarman_io import plan_blocks
  self.assertEqual(plan_blocks([552,588,590,625,653,659,672,673,690]),[(552,1),(588,3),(625,1),(653,21),(690,1)])
  self.assertEqual(plan_blocks([552,588,590,625,653,659,672,673,690],max_span=16,max_gap=4),[(552,1),(588,3),(625,1),(653,1),(659,1),(672,2),(690,1)])
  self.assertEqual(plan_blocks([1,2,3],max_span=2),[(1,2),(3,1)])
 def test_full_profile_reads_in_blocks_and_caches_slow_counters(self):
  values={588:80,672:500,673:300,653:665,552:4,625:958,690:0,590:65365,587:5310,586:1250,541:1420,529:123,526:88,520:20,521:5,514:30,515:41}
  client=self.client(values);c=self.full()
  p,factory=self.run_sample(client,c)
  self.assertEqual(p['pv_w'],800);self.assertAlmostEqual(p['battery_temp_c'],25);self.assertAlmostEqual(p['inverter_temp_c'],42);self.assertAlmostEqual(p['today_pv_kwh'],12.3);self.assertAlmostEqual(p['battery_voltage_v'],53.1)
  first=client.read_holding_registers.call_count
  self.assertLess(first,10)
  client.read_holding_registers.reset_mock();p2,_=self.run_sample(client,c)
  self.assertAlmostEqual(p2['today_pv_kwh'],12.3)
  read=[a for x in client.read_holding_registers.call_args_list for a in range(x.args[0],x.args[0]+x.args[1])]
  self.assertNotIn(529,read,'daily counters are cached for 60 s');self.assertIn(588,read)
  factory.assert_called_once()
 def test_settings_have_safe_limits(self):
  c=self.full();app.validate(c)
  for name,r in c['registers'].items():
   if r.get('writable'):
    self.assertIn('label',r);self.assertLessEqual(r['max'],12000)
  with self.assertRaises(ValueError):app.check_value('prog1_time',c['registers']['prog1_time'],1275,c)
  with self.assertRaises(ValueError):app.check_value('work_mode',c['registers']['work_mode'],3,c)
  with self.assertRaises(ValueError):app.check_value('time_of_use',c['registers']['time_of_use'],7,c)
  self.assertEqual(app.check_value('prog1_time',c['registers']['prog1_time'],2230,c),2230)
 def test_write_uses_same_connection_and_readback(self):
  values={142:0}
  from unittest.mock import MagicMock
  client=MagicMock();client.read_holding_registers.side_effect=lambda a,n:[values.get(i,0) for i in range(a,a+n)]
  def write(a,v):values[a]=v;return v
  client.write_holding_register.side_effect=write
  c=self.full()|{'verified_profile':True,'allow_writes':True};app.save(c)
  import types,sys
  factory=MagicMock(return_value=client)
  with patch('solarman_client.framed_class',side_effect=lambda cls:cls),patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):
   self.assertEqual(app.write_setting('work_mode',1,'test'),1)
  factory.assert_called_once();client.write_holding_register.assert_called_once_with(142,1)
  app.save(dict(app.DEFAULT))

class ForecastAndTelegramTests(NoSleepTestCase):
 def setUp(self):
  super().setUp();app.save(dict(app.DEFAULT))
 def test_forecast_counts_only_outage_hours(self):
  now=time.time();start=datetime.fromtimestamp(now+3600,timezone.utc).isoformat();end=datetime.fromtimestamp(now+3*3600,timezone.utc).isoformat()
  app.save(app.DEFAULT|{'group':'1.1','outages':[{'group':'1.1','start':start,'end':end}],'mode':'modbus_tcp'})
  with app.STATE_LOCK:app.SITE_STATES['default']=dict(online=True,demo=False,error=None,sample=dict(soc=80,load_w=1000,pv_w=0,grid_on=True,t=now,site_id='default'))
  try:
   f=app.status()['forecast'];self.assertAlmostEqual(f['discharge_hours'],2,places=1);self.assertIn('restore_at',f)
  finally:app.SITE_STATES.clear()
 def test_secret_token_hidden_and_not_writable_via_config(self):
  app.save(app.DEFAULT|{'telegram_token':'123:abc'})
  pub=app.public_config();self.assertNotIn('telegram_token',pub);self.assertTrue(pub['telegram_token_set'])
 def test_pairing_flow_and_commands(self):
  app.save(app.DEFAULT|{'telegram_token':'123:abc'})
  with patch.object(app,'telegram_info',return_value={'username':'deye_bot'}):r=app.start_pairing()
  self.assertEqual(r['link'],'https://t.me/deye_bot?start='+r['code'])
  msg=lambda text,uid=42:{'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':text}
  self.assertIn('42',app.handle_bot_message(msg('/start'),{}))
  self.assertIsNone(app.handle_bot_message(msg('/status'),{}),'strangers get no data')
  self.assertIn('Доступ надано',app.handle_bot_message(msg('/start '+r['code']),{}))
  self.assertEqual(app.config()['telegram_users'],[42])
  self.assertIn('недійсний',app.handle_bot_message(msg('/pair '+r['code'],7),{}),'codes are single-use')
  self.assertIn('/status',app.handle_bot_message(msg('/help'),{}))
  with self.assertRaises(ValueError):app.handle_bot_message(msg('/set work_mode 1'),{})
 def test_notify_is_queued_not_blocking(self):
  app.save(app.DEFAULT|{'telegram_token':'123:abc','telegram_users':[1]})
  while not app.TG_OUTBOX.empty():app.TG_OUTBOX.get_nowait()
  with patch.object(app,'tg',side_effect=AssertionError('must not call network inline')):app.notify('hello')
  self.assertEqual(app.TG_OUTBOX.get_nowait(),'hello')

class RateLimitTests(NoSleepTestCase):
 def test_bruteforce_is_throttled(self):
  app.FAILURES.clear()
  def handler():
   h=object.__new__(app.Handler);h.client_address=('192.168.1.77',1);h.headers={'Authorization':'Basic bad'};return h
  codes=[]
  for _ in range(11):
   h=handler()
   with patch.object(h,'send') as send:h.auth();codes.append(send.call_args.args[0])
  self.assertEqual(codes[:10],[401]*10);self.assertEqual(codes[10],429);app.FAILURES.clear()
