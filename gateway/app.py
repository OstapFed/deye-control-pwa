import base64, csv, hashlib, hmac, io, json, math, os, secrets, socket, sqlite3, struct, threading, time, ipaddress
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).parent
STATIC = ROOT.parent
APP_VERSION = '2026.11.06-single-register-1'
DATA = Path(os.environ.get('DEYE_DATA', str(ROOT / 'data')))
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
LOCK = threading.RLock()
DEFAULT = dict(address='', operator='', group='', timezone='Europe/Kyiv', capacity_kwh=10, reserve_soc=20, efficiency=.9, host='', port=502, unit=1, mode='demo', registers={}, verified_profile=False, allow_writes=False, outages=[], schedule_url='', telegram_users=[], warning_minutes=30, target_soc=95, batteries=[], poll_seconds=2, active_site='default', sites=[], logger_serial=0)

def db():
    c = sqlite3.connect(DATA / 'history.sqlite')
    c.execute('CREATE TABLE IF NOT EXISTS samples(t REAL, payload TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS audit(t REAL, actor TEXT, setting TEXT, old TEXT, new TEXT)')
    return c

def config():
    with LOCK:
        p = DATA / 'config.json'
        return DEFAULT | (json.loads(p.read_text()) if p.exists() else {})

def battery_capacity(batteries):
    return sum(float(b['nominal_kwh']) * float(b['usable_pct']) / 100 for b in batteries)

def validate_batteries(batteries):
    if not isinstance(batteries, list) or len(batteries) > 32: raise ValueError('Потрібен список до 32 батарей')
    for b in batteries:
        if not isinstance(b.get('name'), str) or not b['name'].strip() or len(b['name']) > 80: raise ValueError('Назва батареї')
        for key,lo,hi in [('nominal_kwh', .1, 1000), ('usable_pct', 1, 100)]:
            n = float(b[key])
            if not math.isfinite(n) or not lo <= n <= hi: raise ValueError('Некоректна ємність батареї')
    return batteries

def validate(c):
    if not 0 <= int(c['logger_serial']) <= 0xffffffff: raise ValueError('Некоректний серійний номер логера')
    validate_batteries(c['batteries'])
    if not 0 <= float(c['reserve_soc']) < float(c['target_soc']) <= 100: raise ValueError('Потрібно 0 ≤ мінімум SOC < ціль SOC ≤ 100')
    if c['batteries']: c['capacity_kwh'] = battery_capacity(c['batteries'])
    if c['mode'] not in ('demo', 'modbus_tcp', 'solarman_v5'): raise ValueError('Невідомий режим')
    for key, lo, hi in [('capacity_kwh', .1, 10000), ('reserve_soc', 0, 100), ('efficiency', .1, 1), ('port', 1, 65535), ('unit', 1, 247), ('warning_minutes', 1, 1440), ('poll_seconds', 2, 60)]:
        n = float(c[key])
        if not math.isfinite(n) or not lo <= n <= hi: raise ValueError('Некоректне поле: ' + key)
    for x in c['outages']:
        if not isinstance(x.get('group'), str): raise ValueError('Потрібна група')
        if stamp(x['end']) <= stamp(x['start']): raise ValueError('Кінець має бути після початку')
    for name, r in c['registers'].items():
        addresses=[r['address'],*r.get('sum_addresses',[])] + ([r['address_high']] if 'address_high' in r else [])
        if not all(0 <= int(x) <= 65535 for x in addresses): raise ValueError('Адреса регістра')
        if 'bit' in r and not 0<=int(r['bit'])<=15: raise ValueError('Номер біта')
        if r.get('writable') and any(k in r for k in ('bit','clip','sum_addresses','address_high')): raise ValueError('Складені регістри доступні лише для читання')
        if not math.isfinite(float(r.get('scale', 1))) or float(r.get('scale', 1)) == 0: raise ValueError('Масштаб регістра')
        if int(r.get('function', 3)) not in (3, 4): raise ValueError('Функція читання')
        if r.get('writable') and (not math.isfinite(float(r['min'])) or not math.isfinite(float(r['max'])) or float(r['min']) > float(r['max'])): raise ValueError('Межі запису')
    return c

def save(c):
    validate(c)
    with LOCK:
        p = DATA / 'config.tmp'
        p.write_text(json.dumps(c, ensure_ascii=False, indent=2)); p.chmod(0o600)
        p.replace(DATA / 'config.json')

def stamp(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None: raise ValueError('Час має містити UTC-зсув, наприклад +02:00')
    return d.timestamp()

def forecast(soc, load, pv, c, hours):
    available = max(0, (soc - float(c['reserve_soc'])) / 100 * float(c['capacity_kwh']) * float(c['efficiency']))
    net = max(0, load - pv) / 1000
    runtime = available / net if net else None
    projected = max(0, min(100, soc - net * hours / float(c['efficiency']) / float(c['capacity_kwh']) * 100))
    return dict(hours_to_reserve=runtime, projected_soc=round(projected, 1), insufficient=runtime is not None and runtime < hours, assumption='Поточні споживання та генерація залишаються сталими; це оцінка, не прогноз погоди.')

DEVICE_LOCKS = defaultdict(threading.RLock)

class Modbus:
    def __init__(self, c):
        self.c = c; self._batch=False; self._client=None; self._cache={}
        self._device_lock=DEVICE_LOCKS[(c.get('host'),c.get('port'),c.get('unit'),c.get('logger_serial'))]
    def __enter__(self):
        self._device_lock.acquire(); self._batch=True; return self
    def __exit__(self,*args):
        try: self.close()
        finally: self._batch=False; self._cache.clear(); self._device_lock.release()
    def close(self):
        if self._client:
            self._client.disconnect(); self._client=None
    def prefetch(self, registers): pass
    def request(self, function, address, value):
        tid = secrets.randbelow(65536)
        def exact(sock, n):
            b = b''
            while len(b) < n:
                p = sock.recv(n-len(b))
                if not p: raise OSError('З’єднання закрито')
                b += p
            return b
        with socket.create_connection((self.c['host'], int(self.c['port'])), timeout=4) as s:
            s.settimeout(4)
            s.sendall(struct.pack('>HHHBBHH', tid, 0, 6, int(self.c['unit']), function, address, value))
            t, proto, length, unit = struct.unpack('>HHHB', exact(s, 7))
            if t != tid or proto or unit != int(self.c['unit']) or not 2 <= length <= 254: raise OSError('Некоректний Modbus MBAP')
            p = exact(s, length - 1)
            if p[0] != function: raise OSError('Modbus exception / невірна функція')
            if function in (3, 4):
                if len(p) != 4 or p[1] != 2: raise OSError('Некоректна відповідь')
                return struct.unpack('>H', p[2:])[0]
            if p != struct.pack('>BHH', function, address, value): raise OSError('Запис не підтверджено')
    def read(self, r):
        fc=int(r.get('function',3))
        v=self.request(fc,int(r['address']),1)
        bits=16
        if 'address_high' in r:
            v |= self.request(fc,int(r['address_high']),1) << 16; bits=32
        if r.get('signed') and v >= 1 << (bits-1): v -= 1 << bits
        if 'bit' in r: v=(v >> int(r['bit'])) & 1
        for address in r.get('sum_addresses',[]): v += self.request(fc,int(address),1)
        v *= float(r.get('scale',1))
        if r.get('clip')=='positive': v=max(0,v)
        if r.get('clip')=='negative': v=max(0,-v)
        return v

class SolarmanV5(Modbus):
    def _connect(self):
        try:
            from pysolarmanv5 import PySolarmanV5
        except ImportError as e:
            raise ValueError('Встановіть драйвер: python -m pip install -r gateway/requirements.txt') from e
        serial=int(self.c.get('logger_serial',0))
        if not 0<serial<=0xffffffff: raise ValueError('Вкажіть серійний номер логера Solarman (не інвертора)')
        return PySolarmanV5(self.c['host'],serial,port=int(self.c['port']),mb_slave_id=int(self.c['unit']),socket_timeout=5,auto_reconnect=False,verbose=False)
    def _session(self):
        if self._client is None: self._client=self._connect()
        return self._client
    def prefetch(self, registers):
        if not self._batch: return
        grouped=defaultdict(set)
        for r in registers:
            fc=int(r.get('function',3))
            grouped[fc].update([int(r['address']),*map(int,r.get('sum_addresses',[]))])
            if 'address_high' in r: grouped[fc].add(int(r['address_high']))
        client=self._session()
        for fc,addresses in grouped.items():
            reader=client.read_holding_registers if fc==3 else client.read_input_registers
            # The installed logger answered individual words after timing out on blocks.
            # Share one session and cache exact words; never read across unused gaps.
            for address in sorted(addresses):
                try: values=reader(address,1)
                except Exception as error:
                    raise OSError(type(error).__name__+' at register '+str(address)+': '+(str(error) or repr(error))) from error
                if len(values)!=1: raise OSError('Неповна відповідь Solarman, регістр '+str(address))
                self._cache[(fc,address)]=values[0]
    def request(self, function, address, value):
        if function in (3,4) and (function,address) in self._cache: return self._cache[(function,address)]
        client=self._session() if self._batch else self._connect()
        try:
            if function in (3,4):
                reader=client.read_holding_registers if function==3 else client.read_input_registers
                data=reader(address,1)
                if len(data)!=1: raise OSError('Некоректна відповідь Solarman V5')
                return data[0]
            if function==6:
                self._cache.clear()
                actual=client.write_holding_register(address,value)
                if actual!=value: raise OSError('Запис Solarman не підтверджено')
                return actual
            raise ValueError('Непідтримувана функція Solarman')
        finally:
            if not self._batch: client.disconnect()

def inverter(c):
    return SolarmanV5(c) if c['mode']=='solarman_v5' else Modbus(c)

def read_sample(c):
    required=('soc','pv_w','load_w','grid_on')
    optional=('grid_import_w','grid_export_w','solar_direct_w','battery_w')
    missing=[key for key in required if key not in c['registers']]
    if missing: raise ValueError('У профілі відсутні канали: '+', '.join(missing))
    with inverter(c) as m:
        m.prefetch([c['registers'][key] for key in (*required,*optional) if key in c['registers']])
        point={key:m.read(c['registers'][key]) for key in (*required,*optional) if key in c['registers']}
    if not all(math.isfinite(v) for v in point.values()) or not 0<=point['soc']<=100 or min(point['pv_w'],point['load_w'])<0: raise ValueError('Некоректні вимірювання / профіль')
    for key in ('grid_import_w','grid_export_w','solar_direct_w'):
        if key in point and point[key]<0: raise ValueError('Некоректний показник '+key)
    point['grid_on']=bool(point['grid_on'])
    return point

STATE = dict(online=False, demo=True, error='Очікування опитування', sample=None)
STATE_LOCK = threading.RLock()
SITE_STATES = {}

def schedule_state(c, now=None):
    now = time.time() if now is None else now
    intervals = sorted((stamp(x['start']), stamp(x['end'])) for x in c['outages'] if x['group'] == c['group'] and stamp(x['end']) > now)
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    active = next((x for x in merged if x[0] <= now < x[1]), None)
    future = next((x for x in merged if x[0] > now), None)
    on = active[1] if active else future[1] if future else None
    off = future[0] if future else None
    return dict(server_time=now, active=active is not None, next_off_at=off, next_on_at=on,
                seconds_to_off=max(0, off-now) if off is not None else None,
                seconds_to_on=max(0, on-now) if on is not None else None,
                group=c['group'], timezone=c['timezone'], intervals=merged,
                basis='Плановий графік; фактичний стан мережі визначається інвертором')

def duration(seconds):
    if seconds is None: return 'немає даних у графіку'
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}"

def schedule_summary(s=None):
    s = status() if s is None else s
    t = s['schedule']
    return ('Планове відключення триває\n' if t['active'] else '') + 'До наступного вимкнення: ' + duration(t['seconds_to_off']) + '\nДо планового ввімкнення: ' + duration(t['seconds_to_on']) + '\n' + t['basis']

def status():
    c = config(); now = time.time()
    with STATE_LOCK: result = dict(SITE_STATES.get(c['active_site'],STATE))
    result['schedule'] = schedule_state(c, now)
    result['version'] = APP_VERSION
    result['site_id'] = c['active_site']
    result['site_name'] = c['address']
    result['poll_seconds'] = c['poll_seconds']
    intervals = result['schedule']['intervals']
    outage = dict(group=c['group'], start=datetime.fromtimestamp(intervals[0][0], timezone.utc).isoformat(), end=datetime.fromtimestamp(intervals[0][1], timezone.utc).isoformat()) if intervals else None
    result['outage'] = outage
    s = result['sample']
    if s and s.get('site_id','default') != c['active_site']:
        result.update(online=False,sample=None,error='Очікування телеметрії обраного об’єкта'); s=None
    if s and result['online']:
        hours = max(0, (stamp(outage['end']) - now) / 3600) if outage else 1
        result['forecast'] = forecast(s['soc'], s['load_w'], s['pv_w'], c, hours)
    return result

def write_setting(name, value, actor):
    with LOCK:
        c = config(); r = c['registers'].get(name, {})
        if c['mode'] not in ('modbus_tcp','solarman_v5') or not c['verified_profile'] or not c['allow_writes'] or not r.get('writable'): raise ValueError('Запис заблоковано: потрібен перевірений профіль і дозвіл')
        value = float(value)
        if name == 'reserve_soc' and not value < float(c['target_soc']): raise ValueError('Мінімальний SOC має бути нижче цільового')
        if name == 'target_soc' and not value > float(c['reserve_soc']): raise ValueError('Цільовий SOC має бути вище мінімального')
        if not math.isfinite(value) or not float(r['min']) <= value <= float(r['max']): raise ValueError('Значення поза межами профілю')
        raw = value / float(r.get('scale', 1))
        if abs(raw-round(raw)) > 1e-6: raise ValueError('Значення не відповідає кроку регістра')
        raw = round(raw)
        if not (-32768 if r.get('signed') else 0) <= raw <= (32767 if r.get('signed') else 65535): raise ValueError('Переповнення регістра')
        with inverter(c) as m:
            old = m.read(r)
            with db() as d: d.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (time.time(), actor, name, str(old), 'REQUESTED ' + str(value)))
            m.request(6, int(r['address']), raw & 65535)
            actual = m.read(r)
            with db() as d: d.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (time.time(), actor, name, str(old), 'READBACK ' + str(actual)))
        if abs(actual-value) > 1e-6: raise ValueError('Контрольне читання не збігається; перевірте інвертор')
        if name in ('reserve_soc','target_soc'):
            c[name] = actual; save(c)
        return actual

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
def tg(method, data):
    req = Request('https://api.telegram.org/bot' + TOKEN + '/' + method, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=35) as f: response = json.load(f)
    if not response.get('ok'): raise OSError('Telegram API error')
    return response['result']

def notify(message):
    if not TOKEN: return
    for uid in config()['telegram_users']:
        try: tg('sendMessage', dict(chat_id=uid, text=message))
        except Exception: pass

def summary():
    s = status()
    if not s['online']: return 'Немає актуального зв’язку з інвертором\n' + schedule_summary(s)
    p = s['sample']; f = s.get('forecast', {})
    return ('ДЕМО\n' if s['demo'] else '') + f"SOC: {p['soc']:.1f}%\nСонце: {p['pv_w']:.0f} Вт\nНавантаження: {p['load_w']:.0f} Вт\nМережа: {'є' if p['grid_on'] else 'немає'}\nОцінка залишку: {f.get('projected_soc')}%\n" + f.get('assumption', '') + '\n' + schedule_summary(s)

def report(hours=24):
    hours = float(hours)
    if not math.isfinite(hours) or not 0 < hours <= 2160: raise ValueError('Період звіту: 0–2160 год')
    now = time.time()
    with db() as d: rows = d.execute('SELECT t,payload FROM samples WHERE t>=? AND t<=? ORDER BY t', (now-hours*3600,now)).fetchall()
    mode_demo = config()['mode'] == 'demo'
    site_id = config()['active_site']
    samples = [(t, json.loads(p)) for t,p in rows if bool(json.loads(p).get('demo')) == mode_demo and json.loads(p).get('site_id','default') == site_id]
    for _,point in samples:
        if 'battery_w' in point:
            point['battery_charge_w']=max(0,point['battery_w']); point['battery_discharge_w']=max(0,-point['battery_w'])
    keys = ('pv_w', 'load_w', 'grid_import_w', 'grid_export_w', 'solar_direct_w', 'battery_charge_w', 'battery_discharge_w')
    totals = {k.replace('_w','_kwh'):None for k in keys}
    metric_seconds = {k.replace('_w','_kwh'):0 for k in keys}
    covered = 0
    for (t,a),(u,b) in zip(samples,samples[1:]):
        if not 0 < u-t <= 120: continue
        covered += u-t
        for field in keys:
            if field not in a or field not in b: continue
            key=field.replace('_w','_kwh')
            amount=(a[field]+b[field])/2
            if field in ('battery_charge_w','battery_discharge_w') and a.get('battery_w',0)*b.get('battery_w',0)<0:
                va,vb=abs(a['battery_w']),abs(b['battery_w'])
                fraction=va/(va+vb)
                if field=='battery_charge_w': amount=va*fraction/2 if a['battery_w']>0 else vb*(1-fraction)/2
                else: amount=va*fraction/2 if a['battery_w']<0 else vb*(1-fraction)/2
            totals[key]=(totals[key] or 0)+amount*(u-t)/3600000
            metric_seconds[key] += u-t
    # Hour buckets retain averages without bridging missing measurements.
    buckets = {}
    for t,p in samples:
        key = int(t//3600)*3600
        buckets.setdefault(key,[]).append(p)
    series=[]
    for t,group in sorted(buckets.items()):
        point={'t':t,'demo':mode_demo}
        for key in (*keys,'battery_w','soc'):
            values=[x[key] for x in group if key in x]
            point[key]=sum(values)/len(values) if values else None
        series.append(point)
    return dict(samples=len(samples), hours=hours, demo=mode_demo, coverage_hours=covered/3600,
                metric_coverage_hours={k:v/3600 for k,v in metric_seconds.items()}, series=series,
                **{k:round(v,4) if v is not None else None for k,v in totals.items()},
                note='Інтеграція потужностей, пропуски понад 120 с виключені. Відсутній канал не замінюється оцінкою. Покриття різних каналів може відрізнятися.')

def capabilities():
    c=config()
    writable={}
    if c['mode'] in ('modbus_tcp','solarman_v5') and c['verified_profile'] and c['allow_writes']:
        for name,r in c['registers'].items():
            if r.get('writable'): writable[name] = {k:r[k] for k in ('min','max','scale','label','unit') if k in r}
    return dict(version=APP_VERSION,protocol='deye-control-v4',writable=writable,metrics=list(c['registers']),demo=c['mode']=='demo')


def report_message():
    r = report()
    return json.dumps({k:v for k,v in r.items() if k not in ('series','metric_coverage_hours')},ensure_ascii=False)

def bot():
    offset = 0; pending = {}
    while TOKEN:
        try:
            for update in tg('getUpdates', dict(offset=offset, timeout=25, allowed_updates=['message'])):
                offset = update['update_id']+1; m = update.get('message', {}); uid = m.get('from', {}).get('id'); chat = m.get('chat', {})
                if chat.get('type') != 'private' or uid not in config()['telegram_users']: continue
                parts = m.get('text','').split(); cmd = parts[0].split('@')[0] if parts else ''
                try:
                    if cmd == '/status': reply = summary()
                    elif cmd == '/report': reply = report_message()
                    elif cmd == '/schedule': reply = schedule_summary()
                    elif cmd == '/set' and len(parts)==3:
                        nonce=secrets.token_hex(3); pending[uid]=(nonce,time.time()+60,parts[1],float(parts[2])); reply=f'Зміна {parts[1]} = {parts[2]}. Підтвердьте протягом 60 с: /confirm {nonce}'
                    elif cmd == '/confirm' and len(parts)==2:
                        p = pending.pop(uid, None)
                        if not p or p[0]!=parts[1] or p[1]<time.time(): raise ValueError('Підтвердження недійсне')
                        reply = 'Записано: ' + str(write_setting(p[2],p[3],str(uid)))
                    else: reply='/status — показники\n/report — звіт 24 год\n/schedule — найближче відключення\n/set назва значення — зміна\n/confirm код — підтвердження'
                except Exception as e: reply = str(e)
                tg('sendMessage', dict(chat_id=chat['id'], text=reply))
        except Exception: time.sleep(5)

def poll():
    previous = None; previous_site = None; was_online = None; warned = set(); last_schedule=0; last_daily=None
    while True:
        started = time.monotonic()
        c = config()
        if previous_site != c['active_site']:
            previous = None; was_online = None; previous_site = c['active_site']; warned = set()
        try:
            if c['schedule_url'] and time.time()-last_schedule>300:
                last_schedule=time.time()
                try:
                    if not c['schedule_url'].startswith('https://'): raise ValueError('Джерело має бути HTTPS')
                    with urlopen(c['schedule_url'], timeout=10) as f: events=json.loads(f.read(1000000))
                    with LOCK:
                        latest=config(); latest['outages']=events; save(latest); c=latest
                except Exception: notify('Не вдалося оновити графік. Збережений графік може бути застарілим.')
            if c['mode']=='demo': s=dict(soc=72.,pv_w=1400.,load_w=800.,grid_on=True,grid_import_w=0.,grid_export_w=100.,solar_direct_w=800.,battery_w=500.)
            else:
                s=read_sample(c)
            s.update(t=time.time(),demo=c['mode']=='demo',site_id=c['active_site'])
            if config()['active_site'] != c['active_site']: continue
            with STATE_LOCK:
                STATE.update(online=True,demo=s['demo'],error=None,sample=s)
                SITE_STATES[c['active_site']] = dict(STATE)
            with db() as d:
                d.execute('INSERT INTO samples VALUES(?,?)',(s['t'],json.dumps(s)))
                d.execute('DELETE FROM samples WHERE t<?',(time.time()-90*86400,))
            if not s['demo']:
                if was_online is False: notify('Зв’язок з інвертором відновлено')
                if previous is not None and previous != s['grid_on']: notify(('Мережа повернулася\n' if s['grid_on'] else 'Мережа зникла\n')+summary())
                previous=s['grid_on']
                st=status(); o=st['outage']
                if o:
                    key=(o['start'],o['end'],o['group']); delta=stamp(o['start'])-time.time()
                    if 0<delta<=float(c['warning_minutes'])*60 and key not in warned:
                        notify('Наближається планове відключення: '+o['start']+' — '+o['end']+'\n'+summary()); warned.add(key)
                    if st['forecast']['insufficient'] and ('low',key) not in warned:
                        notify('За поточним навантаженням резерву може не вистачити до планового повернення мережі.\n'+summary()); warned.add(('low',key))
                day=datetime.now(timezone.utc).date().isoformat()
                if datetime.now(timezone.utc).hour==18 and last_daily!=day:
                    notify('Добовий звіт\n'+report_message()); last_daily=day
            was_online=True
        except Exception as e:
            with STATE_LOCK:
                STATE.update(online=False,error=str(e) or type(e).__name__)
                SITE_STATES[c['active_site']] = dict(STATE)
            if was_online is True: notify('Втрачено зв’язок з інвертором; прогноз призупинено')
            was_online=False
        time.sleep(max(.2, float(c['poll_seconds'])-(time.monotonic()-started)))

def background_poll():
    previous = {}
    def collect(site):
        identity=site['id']
        try:
            if site['mode'] not in ('modbus_tcp','solarman_v5'): return
            point=read_sample(site)
            point.update(t=time.time(),demo=False,site_id=identity)
            with STATE_LOCK: SITE_STATES[identity]=dict(online=True,demo=False,error=None,sample=point)
            with db() as d: d.execute('INSERT INTO samples VALUES(?,?)',(point['t'],json.dumps(point)))
            old=previous.get(identity)
            if old is not None and old != point['grid_on']: notify(('Мережа повернулася' if point['grid_on'] else 'Мережа зникла')+' · '+site.get('address',identity)+' · SOC '+str(point['soc'])+'%')
            previous[identity]=point['grid_on']
        except Exception as e:
            with STATE_LOCK: SITE_STATES[identity]=dict(online=False,demo=False,error=str(e) or type(e).__name__,sample=SITE_STATES.get(identity,{}).get('sample'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            started=time.monotonic();c=config()
            profiles=[site for site in c['sites'] if site['id']!=c['active_site']]
            list(pool.map(collect,profiles))
            time.sleep(max(.2,float(c['poll_seconds'])-(time.monotonic()-started)))

SITE_FIELDS = ('logger_serial','address','group','host','port','unit','mode','registers','verified_profile','allow_writes','batteries','capacity_kwh','reserve_soc','target_soc','outages','schedule_url')

def site_list():
    c=config()
    sites=[{'id':x['id'],'name':x.get('address') or x['id'],'host':x.get('host',''),'active':x['id']==c['active_site']} for x in c['sites']]
    if not any(x['id']==c['active_site'] for x in sites): sites.insert(0,dict(id=c['active_site'],name=c['address'] or 'Мій дім',host=c['host'],active=True))
    with STATE_LOCK:
        for site in sites:
            st=SITE_STATES.get(site['id'],{})
            point=st.get('sample') or {}
            site.update(online=bool(st.get('online') and time.time()-point.get('t',0)<60),soc=point.get('soc'),updated_at=point.get('t'))
    return sites

def select_site(site_id):
    with LOCK:
        c=config()
        if c['active_site']==site_id: return dict(ok=True,site_id=site_id)
        target=next((x for x in c['sites'] if x['id']==site_id),None)
        if target is None: raise ValueError('Невідоме домогосподарство')
        current={k:c[k] for k in SITE_FIELDS}; current['id']=c['active_site']
        sites=[x for x in c['sites'] if x['id']!=c['active_site']]+[current]
        c.update({k:target[k] for k in SITE_FIELDS if k in target}); c['active_site']=site_id;c['sites']=sites;save(c)
        with STATE_LOCK: STATE.update(online=False,sample=None,error='Підключення до обраного домогосподарства')
        return dict(ok=True,site_id=site_id)

def add_site(body):
    host=str(body.get('host','')).strip()
    ip=ipaddress.ip_address(host)
    if not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')): raise ValueError('Для знайдених інверторів потрібна локальна IPv4 адреса')
    with LOCK:
        c=config()
        if len(c['sites']) >= 64: raise ValueError('До 64 домогосподарств на сервер')
        site={k:DEFAULT[k] for k in SITE_FIELDS};site.update(id=secrets.token_hex(6),host=host,port=int(body.get('port',502)),address=str(body.get('name') or host)[:160],mode='solarman_v5' if int(body.get('port',502))==8899 else 'modbus_tcp',registers={},verified_profile=False,allow_writes=False)
        c['sites']=[*c['sites'],site];save(c)
        return dict(ok=True,site_id=site['id'])

def discover_network(cidr):
    network=ipaddress.ip_network(cidr,strict=False)
    allowed=[ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    if network.version!=4 or network.num_addresses>256 or not any(network.subnet_of(x) for x in allowed): raise ValueError('Дозволена локальна IPv4-підмережа /24 або менша (до 256 адрес)')
    jobs=[(str(ip),port) for ip in network.hosts() for port in (502,8899)]
    def probe(item):
        host,port=item
        try:
            with socket.create_connection((host,port),timeout=.25):
                return dict(host=host,port=port,protocol='Modbus TCP — кандидат' if port==502 else 'Solarman V5 — кандидат; потрібен серійний номер логера',supported=True)
        except OSError: return None
    with ThreadPoolExecutor(max_workers=48) as pool: found=[x for x in pool.map(probe,jobs) if x]
    return dict(devices=found,note='Перевірено лише доступність TCP-портів. Модель, серійний номер і карта регістрів не визначалися. Записів в обладнання не було.')

PASSWORD=os.environ.get('DEYE_PASSWORD','')
class Handler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # A browser may cancel static fetches or close a stream during navigation.
            # Leave unrelated OSErrors visible so server failures are still diagnosed.
            return
    def log_message(self,*args): pass
    def end_headers(self):
        origin = self.headers.get('Origin', '')
        allowed = [x.strip() for x in os.environ.get('DEYE_WEB_ORIGINS','https://ostapfed.github.io').split(',') if x.strip()]
        if origin and origin in allowed:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Deye-Request')
        super().end_headers()
    def do_OPTIONS(self):
        self.send_response(204); self.end_headers()
    def send(self, code, body, kind='application/json'):
        data=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header('Content-Type',kind+'; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def auth(self):
        expected='Basic '+base64.b64encode(('admin:'+PASSWORD).encode()).decode()
        if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
            self.send_response(401); self.send_header('WWW-Authenticate','Basic realm="Deye"'); self.end_headers(); return False
        return True
    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        self.path = parsed.path
        assets = {name: (name.lstrip('/'), mime) for name,mime in [
            ('/manifest.webmanifest','application/manifest+json'),('/sw.js','text/javascript'),
            ('/app.js','text/javascript'),('/core.js','text/javascript'),('/style.css','text/css'),
            ('/deye-final-192.png','image/png'),('/deye-final-512.png','image/png'),('/deye-final-apple.png','image/png')]}
        if self.path in assets:
            filename,mime=assets[self.path]
            return self.send(200,(STATIC/filename).read_bytes(),mime)
        if self.path=='/': return self.send(200,(STATIC/'index.html').read_bytes(),'text/html')
        if not self.auth(): return
        if self.path=='/api/stream':
            self.send_response(200); self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.end_headers()
            try:
                for _ in range(150):
                    self.wfile.write(('data: '+json.dumps(status(),ensure_ascii=False)+'\n\n').encode());self.wfile.flush();time.sleep(2)
            except (BrokenPipeError,ConnectionResetError,OSError): pass
            return
        if self.path=='/api/sites': return self.send(200,json.dumps(site_list(),ensure_ascii=False))
        if self.path=='/api/config': return self.send(200,json.dumps(config(),ensure_ascii=False))
        if self.path=='/api/status': return self.send(200,json.dumps(status(),ensure_ascii=False))
        if self.path=='/api/report':
            try: return self.send(200,json.dumps(report(query.get('hours',[24])[0]),ensure_ascii=False))
            except ValueError as e: return self.send(400,json.dumps({'error':str(e)},ensure_ascii=False))
        if self.path=='/api/capabilities': return self.send(200,json.dumps(capabilities(),ensure_ascii=False))
        if self.path=='/api/audit':
            with db() as d: rows=d.execute('SELECT t,actor,setting,old,new FROM audit ORDER BY t DESC LIMIT 100').fetchall()
            return self.send(200,json.dumps([dict(zip(('t','actor','setting','old','new'),row)) for row in rows],ensure_ascii=False))
        if self.path=='/api/history.csv':
            with db() as d: rows=d.execute('SELECT t,payload FROM samples ORDER BY t').fetchall()
            out=io.StringIO(); w=csv.writer(out); w.writerow(['UTC','SOC','PV W','Load W','Grid','Demo','Import W','Export W','Direct solar W','Battery W'])
            for t,p in rows:
                s=json.loads(p); w.writerow([datetime.fromtimestamp(t,timezone.utc).isoformat(),s['soc'],s['pv_w'],s['load_w'],s['grid_on'],s['demo'],s.get('grid_import_w'),s.get('grid_export_w'),s.get('solar_direct_w'),s.get('battery_w')])
            return self.send(200,out.getvalue(),'text/csv')
        self.send(404,'{}')
    def do_POST(self):
        if not self.auth(): return
        # JSON-only + custom header prevents cross-origin form submissions.
        if self.headers.get('X-Deye-Request')!='1' or self.headers.get('Content-Type')!='application/json': return self.send(403,'{}')
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=1000000: raise ValueError('Розмір запиту')
            body=json.loads(self.rfile.read(n))
            if self.path=='/api/config':
                if set(body)-set(DEFAULT): raise ValueError('Невідомі поля')
                with LOCK: save(config() | body)
                result={'ok':True}
            elif self.path=='/api/discover': result=discover_network(body['cidr'])
            elif self.path=='/api/sites': result=add_site(body)
            elif self.path=='/api/site/select': result=select_site(body['site_id'])
            elif self.path=='/api/write':
                if body.get('confirm') is not True: raise ValueError('Потрібне підтвердження')
                result={'value':write_setting(body['name'],body['value'],'web')}
            else: return self.send(404,'{}')
            self.send(200,json.dumps(result))
        except Exception as e: self.send(400,json.dumps({'error':str(e)},ensure_ascii=False))

def main():
    if len(PASSWORD)<12: raise SystemExit('Задайте DEYE_PASSWORD довжиною щонайменше 12 символів')
    db().close()
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=background_poll,daemon=True).start()
    if TOKEN: threading.Thread(target=bot,daemon=True).start()
    ThreadingHTTPServer((os.environ.get('DEYE_BIND','127.0.0.1'),int(os.environ.get('DEYE_PORT','8080'))),Handler).serve_forever()
if __name__=='__main__': main()
