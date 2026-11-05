import importlib.util,os,tempfile,unittest,json,time
from pathlib import Path
from unittest.mock import patch
os.environ['DEYE_DATA']=tempfile.mkdtemp()
spec=importlib.util.spec_from_file_location('gateway',Path(__file__).parents[1]/'gateway/app.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
class GatewayTests(unittest.TestCase):
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

class HouseholdTests(unittest.TestCase):
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

class SolarmanTests(unittest.TestCase):
 def test_v5_factory_and_read(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.read_holding_registers.return_value=[720]
  factory=MagicMock(return_value=client)
  c=app.DEFAULT|{'mode':'solarman_v5','host':'192.168.1.10','port':8899,'logger_serial':1234567890}
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):
   self.assertEqual(app.inverter(c).read({'address':100,'scale':.1}),72)
  factory.assert_called_once_with('192.168.1.10',1234567890,port=8899,mb_slave_id=1,socket_timeout=5,auto_reconnect=False,verbose=False)
  client.disconnect.assert_called_once()
 def test_disconnect_on_error(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.read_input_registers.side_effect=OSError('timeout')
  c=app.DEFAULT|{'mode':'solarman_v5','logger_serial':123}
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}):
   with self.assertRaises(OSError):app.inverter(c).read({'address':100,'function':4})
  client.disconnect.assert_called_once()
 def test_write_echo_required(self):
  from unittest.mock import MagicMock
  import types,sys
  client=MagicMock();client.write_holding_register.return_value=10
  c=app.DEFAULT|{'mode':'solarman_v5','logger_serial':123}
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}):
   with self.assertRaises(OSError):app.inverter(c).request(6,100,20)
  client.disconnect.assert_called_once()

class CompositeProfileTests(unittest.TestCase):
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
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(return_value=client))}),patch.dict(os.environ,{'DEYE_DATA':dest}),patch.object(sys,'argv',['configure','--host','192.168.1.10','--serial','123','--apply']),patch('builtins.print'):
   self.assertEqual(m.main(),0)
  c=json.loads((Path(dest)/'config.json').read_text());self.assertFalse(c['allow_writes']);self.assertFalse(c['verified_profile']);self.assertEqual(c['registers']['soc']['address'],588);client.write_holding_register.assert_not_called()

class HttpDisconnectTests(unittest.TestCase):
 def test_browser_disconnects_do_not_escape(self):
  handler=object.__new__(app.Handler)
  for error in (ConnectionAbortedError(10053,'browser aborted'),ConnectionResetError('reset'),BrokenPipeError('closed')):
   with patch.object(app.BaseHTTPRequestHandler,'handle',side_effect=error):handler.handle()
 def test_other_errors_are_not_hidden(self):
  handler=object.__new__(app.Handler)
  with patch.object(app.BaseHTTPRequestHandler,'handle',side_effect=OSError('server error')):
   with self.assertRaises(OSError):handler.handle()

class BatchedSolarmanTests(unittest.TestCase):
 def profile(self):
  profile=json.loads((Path(__file__).parents[1]/'gateway/profiles/deye_sg04lp3_readonly.json').read_text())
  return app.DEFAULT|{'mode':'solarman_v5','logger_serial':123,'registers':profile}
 def test_one_connection_per_sample_and_cached_directions(self):
  from unittest.mock import MagicMock
  import types,sys
  values={588:80,672:0,673:0,653:665,659:0,552:4,625:958,690:0,590:65365}
  client=MagicMock();client.read_holding_registers.side_effect=lambda start,count:[values.get(i,0) for i in range(start,start+count)]
  factory=MagicMock(return_value=client)
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):p=app.read_sample(self.profile())
  self.assertEqual(p['soc'],80);self.assertEqual(p['load_w'],665);self.assertEqual(p['grid_import_w'],958);self.assertEqual(p['grid_export_w'],0);self.assertEqual(p['battery_w'],171)
  factory.assert_called_once();client.disconnect.assert_called_once();self.assertLessEqual(client.read_holding_registers.call_count,4)
 def test_failure_closes_session_and_next_cycle_reconnects(self):
  from unittest.mock import MagicMock
  import types,sys
  bad=MagicMock();bad.read_holding_registers.side_effect=TimeoutError('logger timeout')
  good=MagicMock();good.read_holding_registers.side_effect=lambda start,count:[50 if i==588 else 0 for i in range(start,start+count)]
  factory=MagicMock(side_effect=[bad,good])
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=factory)}):
   with self.assertRaises(TimeoutError):app.read_sample(self.profile())
   self.assertEqual(app.read_sample(self.profile())['soc'],50)
  bad.disconnect.assert_called_once();good.disconnect.assert_called_once()

class SetupRecoveryTests(unittest.TestCase):
 def module(self):
  spec=importlib.util.spec_from_file_location('configure_retry',Path(__file__).parents[1]/'gateway/configure_deye.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_empty_timeout_has_type_and_stage(self):
  import types,sys
  from unittest.mock import MagicMock
  m=self.module();args=types.SimpleNamespace(host='192.168.1.10',serial=123,unit=1,expected_inverter_serial=None)
  with patch.dict(sys.modules,{'pysolarmanv5':types.SimpleNamespace(PySolarmanV5=MagicMock(side_effect=TimeoutError()))}),patch('builtins.print'):
   with self.assertRaisesRegex(RuntimeError,'TimeoutError at connecting to logger'):m.identify_and_read(args,1)
 def test_setup_retries_transient_error(self):
  import sys
  m=self.module()
  with patch.object(sys,'argv',['configure','--host','192.168.1.10','--serial','123']),patch.object(m,'identify_and_read',side_effect=[RuntimeError('TimeoutError at identity'),{'soc':80}]) as reader,patch.object(m.time,'sleep') as sleep,patch('builtins.print'):
   self.assertEqual(m.main(),0)
  self.assertEqual(reader.call_count,2);sleep.assert_called_once_with(2)
