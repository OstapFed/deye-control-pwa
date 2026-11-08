import base64, csv, hmac, io, json, math, os, queue, secrets, socket, sqlite3, struct, threading, time, ipaddress
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque
from contextlib import contextmanager
from solarman_io import PacedReader
from solarman_client import create_v5_client
from config_storage import read_config
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).parent
STATIC = ROOT.parent
APP_VERSION = '2026.11.08-fc16-write-4'
DATA = Path(os.environ.get('DEYE_DATA', str(ROOT / 'data')))
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
LOCK = threading.RLock()
DEFAULT = dict(address='', operator='', group='', timezone='Europe/Kyiv', capacity_kwh=10, reserve_soc=20, efficiency=.9, host='', port=502, unit=1, mode='demo', registers={}, verified_profile=False, allow_writes=False, outages=[], schedule_url='', telegram_users=[], telegram_token='', daily_report_hour=20, warning_minutes=30, target_soc=95, batteries=[], poll_seconds=2, active_site='default', sites=[], logger_serial=0, persistent_connection=True)
PROFILES = ROOT / 'profiles'

def log(*parts):
    """One line to stdout; the Windows service redirects it to data/server.log."""
    try: print(time.strftime('%Y-%m-%d %H:%M:%S'), *parts, flush=True)
    except Exception: pass
TELEMETRY_REQUIRED = ('soc', 'pv_w', 'load_w', 'grid_on')
SECRET_FIELDS = ('telegram_token',)

# ---------------------------------------------------------------- storage

def db():
    c = sqlite3.connect(DATA / 'history.sqlite', timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS samples(t REAL, payload TEXT)')
    c.execute('CREATE INDEX IF NOT EXISTS samples_t ON samples(t)')
    c.execute('CREATE TABLE IF NOT EXISTS audit(t REAL, actor TEXT, setting TEXT, old TEXT, new TEXT)')
    return c

@contextmanager
def database():
    """Commit on success and always close: plain ``with sqlite3.connect()`` leaves handles open."""
    c = db()
    try:
        with c: yield c
    finally: c.close()

_CONFIG_CACHE = {'key': None, 'value': None}

def config():
    with LOCK:
        p = DATA / 'config.json'
        try:
            st = p.stat(); key = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = None
        if key is None: return dict(DEFAULT)
        if _CONFIG_CACHE['key'] != key:
            _CONFIG_CACHE.update(key=key, value=read_config(p))
        return DEFAULT | json.loads(json.dumps(_CONFIG_CACHE['value']))

def public_config(c=None):
    c = config() if c is None else c
    out = {k: v for k, v in c.items() if k not in SECRET_FIELDS}
    out['telegram_token_set'] = bool(c.get('telegram_token'))
    return out

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

def validate_register(name, r):
    addresses=[r['address'],*r.get('sum_addresses',[])] + ([r['address_high']] if 'address_high' in r else [])
    if not all(0 <= int(x) <= 65535 for x in addresses): raise ValueError('Адреса регістра: ' + name)
    if 'bit' in r and not 0<=int(r['bit'])<=15: raise ValueError('Номер біта')
    if r.get('writable') and any(k in r for k in ('clip','sum_addresses','address_high','offset')): raise ValueError('Складені регістри доступні лише для читання')
    if r.get('kind') not in (None, 'clock'): raise ValueError('Тип регістра: ' + name)
    if r.get('risk') not in (None, 'danger'): raise ValueError('Рівень ризику: ' + name)
    if not math.isfinite(float(r.get('scale', 1))) or float(r.get('scale', 1)) == 0: raise ValueError('Масштаб регістра')
    if not math.isfinite(float(r.get('offset', 0))): raise ValueError('Зсув регістра')
    if not 0 <= float(r.get('every', 0)) <= 3600: raise ValueError('Інтервал читання регістра')
    if int(r.get('function', 3)) not in (3, 4): raise ValueError('Функція читання')
    if r.get('writable') and r.get('kind') != 'clock':
        if not math.isfinite(float(r['min'])) or not math.isfinite(float(r['max'])) or float(r['min']) > float(r['max']): raise ValueError('Межі запису')
        if 'options' in r and (not isinstance(r['options'], dict) or not r['options'] or not all(math.isfinite(float(k)) for k in r['options'])): raise ValueError('Варіанти значень: ' + name)
        if r.get('format') not in (None, 'hhmm'): raise ValueError('Формат регістра')

def validate(c):
    if not 0 <= int(c['logger_serial']) <= 0xffffffff: raise ValueError('Некоректний серійний номер логера')
    validate_batteries(c['batteries'])
    if not 0 <= float(c['reserve_soc']) < float(c['target_soc']) <= 100: raise ValueError('Потрібно 0 ≤ мінімум SOC < ціль SOC ≤ 100')
    if c['batteries']: c['capacity_kwh'] = battery_capacity(c['batteries'])
    if c['mode'] not in ('demo', 'modbus_tcp', 'solarman_v5'): raise ValueError('Невідомий режим')
    for key, lo, hi in [('capacity_kwh', .1, 10000), ('reserve_soc', 0, 100), ('efficiency', .1, 1), ('port', 1, 65535), ('unit', 1, 247), ('warning_minutes', 1, 1440), ('poll_seconds', 2, 60), ('daily_report_hour', -1, 23)]:
        n = float(c[key])
        if not math.isfinite(n) or not lo <= n <= hi: raise ValueError('Некоректне поле: ' + key)
    if not isinstance(c['telegram_users'], list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in c['telegram_users']): raise ValueError('Telegram ID мають бути числами')
    c['telegram_users'] = sorted(set(c['telegram_users']))
    for x in c['outages']:
        if not isinstance(x.get('group'), str): raise ValueError('Потрібна група')
        if stamp(x['end']) <= stamp(x['start']): raise ValueError('Кінець має бути після початку')
    for name, r in c['registers'].items(): validate_register(name, r)
    return c

def save(c):
    validate(c)
    with LOCK:
        p = DATA / 'config.tmp'
        p.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8'); p.chmod(0o600)
        p.replace(DATA / 'config.json')
        _CONFIG_CACHE['key'] = None

def update_config(**changes):
    with LOCK:
        c = config(); c.update(changes); save(c); return c

def stamp(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None: raise ValueError('Час має містити UTC-зсув, наприклад +02:00')
    return d.timestamp()

def local_now(c):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(c.get('timezone') or 'Europe/Kyiv'))
    except Exception:
        # Windows Python without the tzdata package: Kyiv winter time is a safe fallback.
        return datetime.now(timezone(timedelta(hours=2)))

def clock(t, c=None):
    c = config() if c is None else c
    zone = local_now(c).tzinfo
    return datetime.fromtimestamp(t, zone).strftime('%d.%m %H:%M')

# ---------------------------------------------------------------- forecast

def forecast(soc, load, pv, c, hours):
    """Battery estimate after ``hours`` of discharge at constant load/PV."""
    available = max(0, (soc - float(c['reserve_soc'])) / 100 * float(c['capacity_kwh']) * float(c['efficiency']))
    net = max(0, load - pv) / 1000
    runtime = available / net if net else None
    projected = max(0, min(100, soc - net * hours / float(c['efficiency']) / float(c['capacity_kwh']) * 100))
    return dict(hours_to_reserve=runtime, projected_soc=round(projected, 1), discharge_hours=round(hours, 2), insufficient=runtime is not None and runtime < hours, assumption='Поточні споживання та генерація залишаються сталими; це оцінка, не прогноз погоди.')

# ---------------------------------------------------------------- inverter I/O

DEVICE_LOCKS = defaultdict(threading.RLock)
SESSIONS = {}
SLOW_CACHE = {}

def device_key(c):
    return (c.get('host'), int(c.get('port') or 0), int(c.get('unit') or 1), int(c.get('logger_serial') or 0))

def reset_sessions():
    for reader in list(SESSIONS.values()): reader.close()
    SESSIONS.clear(); SLOW_CACHE.clear()

def decode(r, raw):
    """raw: callable(address)->word. Applies the register description."""
    v = raw(int(r['address'])); bits = 16
    if 'address_high' in r:
        v |= raw(int(r['address_high'])) << 16; bits = 32
    if r.get('signed') and v >= 1 << (bits-1): v -= 1 << bits
    if 'bit' in r: v = (v >> int(r['bit'])) & 1
    for address in r.get('sum_addresses', []): v += raw(int(address))
    v = (v - float(r.get('offset', 0))) * float(r.get('scale', 1))
    if r.get('clip') == 'positive': v = max(0, v)
    if r.get('clip') == 'negative': v = max(0, -v)
    return round(v, 6)

def register_addresses(r):
    return [int(r['address']), *map(int, r.get('sum_addresses', []))] + ([int(r['address_high'])] if 'address_high' in r else [])

class Modbus:
    def __init__(self, c):
        self.c = c; self._batch=False; self._cache={}
        self._device_lock=DEVICE_LOCKS[device_key(c)]
    def __enter__(self):
        self._device_lock.acquire(); self._batch=True; return self
    def __exit__(self,*args):
        try: self.close()
        finally: self._batch=False; self._cache.clear(); self._device_lock.release()
    def close(self): pass
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
        fc = int(r.get('function', 3))
        return decode(r, lambda address: self.request(fc, address, 1))
    def write_many(self, address, values):
        payload = b''.join(struct.pack('>H', v) for v in values)
        tid = secrets.randbelow(65536); unit = int(self.c['unit'])
        pdu = struct.pack('>BHHB', 16, address, len(values), len(payload)) + payload
        with socket.create_connection((self.c['host'], int(self.c['port'])), timeout=4) as s:
            s.settimeout(4); s.sendall(struct.pack('>HHHB', tid, 0, len(pdu)+1, unit) + pdu)
            reply = s.recv(260)
            if len(reply) < 12 or reply[7] != 16: raise OSError('Запис регістрів не підтверджено (Modbus exception)')

class SolarmanV5(Modbus):
    """One shared, paced logger connection per device; kept open between polls."""
    def _reader(self):
        key = device_key(self.c)
        reader = SESSIONS.get(key)
        if reader is None:
            reader = SESSIONS[key] = PacedReader(self._connect, on_retry=lambda address, attempt, reason: log('logger', self.c.get('host'), 'register', address, 'attempt', attempt, reason))
        return reader
    def close(self):
        if not self.c.get('persistent_connection', True):
            reader = SESSIONS.pop(device_key(self.c), None)
            if reader: reader.close()
    def _connect(self):
        try:
            import pysolarmanv5  # noqa: F401
        except ImportError as e:
            raise ValueError('Встановіть драйвер: python -m pip install -r gateway/requirements.txt') from e
        serial=int(self.c.get('logger_serial',0))
        if not 0<serial<=0xffffffff: raise ValueError('Вкажіть серійний номер логера Solarman (не інвертора)')
        return create_v5_client(self.c['host'],serial,port=int(self.c['port']),mb_slave_id=int(self.c['unit']),socket_timeout=4,auto_reconnect=False,verbose=False)
    def write_many(self, address, values):
        with self._device_lock:
            self._cache.clear()
            reader=self._reader()
            try: reader.write_many(address, values)
            finally:
                if not self._batch: self.close()

    def prefetch(self, registers):
        if not self._batch: return
        grouped=defaultdict(set)
        for r in registers: grouped[int(r.get('function',3))].update(register_addresses(r))
        reader=self._reader()
        for fc,addresses in grouped.items():
            for address,value in reader.read_many(sorted(addresses),fc).items(): self._cache[(fc,address)]=value
    def request(self, function, address, value):
        with self._device_lock:
            if function in (3,4) and (function,address) in self._cache: return self._cache[(function,address)]
            reader=self._reader()
            try:
                if function in (3,4): return reader.read(address,1,function)[0]
                if function==6:
                    # Deye accepts holding-register writes only as FC16 (write multiple registers);
                    # FC06 is answered with Modbus exception 05 (Acknowledge). Readback verifies the value.
                    self._cache.clear()
                    reader.write_many(address,[value])
                    return value
                raise ValueError('Непідтримувана функція Solarman')
            finally:
                if not self._batch: self.close()

def write_registers(m, address, values):
    """Consecutive-register write (FC16) on either transport. Deye rejects FC06 writes."""
    m.write_many(address, values)

def inverter(c):
    return SolarmanV5(c) if c['mode']=='solarman_v5' else Modbus(c)

def telemetry_channels(c):
    return {k: r for k, r in c['registers'].items() if not r.get('writable')}

def read_sample(c):
    channels = telemetry_channels(c)
    missing=[key for key in TELEMETRY_REQUIRED if key not in channels]
    if missing: raise ValueError('У профілі відсутні канали: '+', '.join(missing))
    collection_started=time.time(); dev=device_key(c)
    due={}; cached={}
    for key, r in channels.items():
        every=float(r.get('every',0)); hit=SLOW_CACHE.get((dev,key))
        if every and hit and collection_started-hit[0] < every: cached[key]=hit[1]
        else: due[key]=r
    with inverter(c) as m:
        m.prefetch(list(due.values()))
        point={}
        for key, r in due.items():
            try: point[key]=m.read(r)
            except Exception:
                if key in TELEMETRY_REQUIRED: raise
    for key in due:
        if key in point and float(channels[key].get('every',0)): SLOW_CACHE[(dev,key)]=(collection_started,point[key])
    point.update(cached)
    for key in list(point):
        if not math.isfinite(point[key]):
            if key in TELEMETRY_REQUIRED: raise ValueError('Некоректні вимірювання / профіль')
            del point[key]
    if not 0<=point['soc']<=100 or min(point['pv_w'],point['load_w'])<0: raise ValueError('Некоректні вимірювання / профіль')
    for key in ('grid_import_w','grid_export_w','solar_direct_w'):
        if key in point and point[key]<0: raise ValueError('Некоректний показник '+key)
    point['grid_on']=bool(point['grid_on'])
    point.update(t=collection_started,collection_seconds=round(time.time()-collection_started,2))
    return point

def setting_definitions(c=None):
    c = config() if c is None else c
    out = {}
    for name, r in c['registers'].items():
        if r.get('writable'): out[name] = {k: r[k] for k in ('min','max','scale','step','label','unit','options','format','group','risk','kind') if k in r}
    return out

SETTINGS = dict(values={}, running=False, done=0, total=0, error=None, updated_at=None)
SETTINGS_LOCK = threading.Lock()

def decode_setting(r, raw):
    if r.get('kind') == 'clock':
        w = [raw[int(r['address']) + i] for i in range(3)]
        return f"{2000 + (w[0] >> 8):04d}-{w[0] & 255:02d}-{w[1] >> 8:02d} {w[1] & 255:02d}:{w[2] >> 8:02d}:{w[2] & 255:02d}"
    return decode(r, lambda address: raw[address])

def read_settings(progress=None):
    """Current values of writable registers (read-only).

    Reads one small block at a time and releases the logger between blocks,
    so live polling keeps running while ~100 settings are fetched.
    """
    c = config()
    if c['mode'] not in ('modbus_tcp','solarman_v5'): raise ValueError('Потрібне реальне підключення')
    regs = {k: r for k, r in c['registers'].items() if r.get('writable')}
    addresses = sorted({int(r['address']) + i for r in regs.values() for i in range(3 if r.get('kind') == 'clock' else 1)})
    from solarman_io import plan_blocks
    blocks = plan_blocks(addresses)
    raw = {}; values = {}
    for n, (start, count) in enumerate(blocks, 1):
        wanted = [a for a in addresses if start <= a < start + count]
        try:
            with inverter(c) as m:
                m.prefetch([dict(address=a) for a in wanted])
                for a in wanted: raw[a] = m.request(3, a, 1)
        except Exception as e:
            log('settings block', start, 'failed:', type(e).__name__, e)
        for k, r in regs.items():
            if k not in values:
                try: values[k] = decode_setting(r, raw)
                except KeyError: pass
        if progress: progress(n, len(blocks), dict(values))
        time.sleep(.2)
    for k in regs: values.setdefault(k, None)
    return values

def settings_job():
    def progress(done, total, values):
        with SETTINGS_LOCK: SETTINGS.update(done=done, total=total, values=values)
    try:
        values = read_settings(progress)
        with SETTINGS_LOCK: SETTINGS.update(values=values, error=None, updated_at=time.time())
    except Exception as e:
        with SETTINGS_LOCK: SETTINGS.update(error=str(e))
    finally:
        with SETTINGS_LOCK: SETTINGS['running'] = False

def start_settings_read():
    with SETTINGS_LOCK:
        if not SETTINGS['running']:
            SETTINGS.update(running=True, done=0, total=0, error=None)
            threading.Thread(target=settings_job, daemon=True).start()
    return settings_state()

def settings_state():
    with SETTINGS_LOCK: return dict(SETTINGS, definitions=setting_definitions())

def check_value(name, r, value, c):
    value = float(value)
    if not math.isfinite(value) or not float(r['min']) <= value <= float(r['max']): raise ValueError('Значення поза межами профілю')
    if 'options' in r and not any(abs(float(k) - value) < 1e-9 for k in r['options']): raise ValueError('Недопустимий варіант значення')
    step = float(r.get('step') or 0)
    if step and abs(value / step - round(value / step)) > 1e-6: raise ValueError('Значення не відповідає кроку ' + str(step))
    if r.get('format') == 'hhmm' and (value != int(value) or int(value) % 100 > 59 or int(value) // 100 > 23): raise ValueError('Час у форматі ГГХХ, наприклад 2230')
    if name == 'reserve_soc' and not value < float(c['target_soc']): raise ValueError('Мінімальний SOC має бути нижче цільового')
    if name == 'target_soc' and not value > float(c['reserve_soc']): raise ValueError('Цільовий SOC має бути вище мінімального')
    return value

def require_writes(c, r):
    if c['mode'] not in ('modbus_tcp','solarman_v5') or not c['verified_profile'] or not c['allow_writes'] or not r.get('writable'): raise ValueError('Запис заблоковано: потрібен перевірений профіль і дозвіл')

def sync_clock(actor):
    """Set the inverter clock to this computer's local time (3 packed registers, FC16)."""
    with LOCK:
        c = config()
        name, r = next(((k, x) for k, x in c['registers'].items() if x.get('kind') == 'clock'), (None, {}))
        require_writes(c, r)
        now = local_now(c).replace(microsecond=0)
        words = [((now.year - 2000) << 8) | now.month, (now.day << 8) | now.hour, (now.minute << 8) | now.second]
        with inverter(c) as m:
            write_registers(m, int(r['address']), words)
            back = [m.request(3, int(r['address']) + i, 1) for i in range(3)]
        with database() as d: d.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (time.time(), actor, name, '', 'CLOCK ' + now.isoformat()))
        if back[:2] != words[:2]: raise ValueError('Годинник не підтверджено читанням')
        log('clock synced', now.isoformat(), 'by', actor)
        with SETTINGS_LOCK: SETTINGS['values'] = dict(SETTINGS['values'], **{name: now.strftime('%Y-%m-%d %H:%M:%S')})
        return now.strftime('%Y-%m-%d %H:%M:%S')

def write_setting(name, value, actor, danger_ok=False):
    with LOCK:
        c = config(); r = c['registers'].get(name, {})
        require_writes(c, r)
        if r.get('kind') == 'clock': raise ValueError('Годинник змінюється командою синхронізації')
        if r.get('risk') == 'danger' and not danger_ok: raise ValueError('Небезпечний параметр: потрібне окреме підтвердження в панелі')
        value = check_value(name, r, value, c)
        raw = value / float(r.get('scale', 1))
        if abs(raw-round(raw)) > 1e-6: raise ValueError('Значення не відповідає кроку регістра')
        raw = round(raw)
        if not (-32768 if r.get('signed') else 0) <= raw <= (32767 if r.get('signed') else 65535): raise ValueError('Переповнення регістра')
        with inverter(c) as m:
            old = m.read(r)
            with database() as d: d.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (time.time(), actor, name, str(old), 'REQUESTED ' + str(value)))
            if 'bit' in r:
                # Read-modify-write: only the one bit changes, the other flags in the word stay as they are.
                if raw not in (0, 1): raise ValueError('Бітовий параметр приймає 0 або 1')
                word = m.request(3, int(r['address']), 1); bit = int(r['bit'])
                raw = (word & ~(1 << bit)) | (raw << bit)
            write_registers(m, int(r['address']), [raw & 65535])
            actual = m.read(r)
            with database() as d: d.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (time.time(), actor, name, str(old), 'READBACK ' + str(actual)))
        log('write', name, old, '->', actual, 'by', actor)
        if abs(actual-value) > 1e-6: raise ValueError('Контрольне читання не збігається; перевірте інвертор')
        with SETTINGS_LOCK: SETTINGS['values'] = dict(SETTINGS['values'], **{name: actual})
        if name in ('reserve_soc','target_soc'):
            c[name] = actual; save(c)
        return actual

def apply_profile(name):
    if name not in ('deye_sg04lp3', 'deye_sg04lp3_readonly'): raise ValueError('Невідомий профіль')
    registers = json.loads((PROFILES / (name + '.json')).read_text(encoding='utf-8'))
    # A new map is never trusted for writes until the owner confirms it again.
    update_config(registers=registers, verified_profile=False, allow_writes=False)
    return dict(ok=True, registers=len(registers))

# ---------------------------------------------------------------- state and schedule

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
        # Battery only discharges while the grid is off: from max(now, outage start) until power returns.
        hours = max(0, (intervals[0][1] - max(now, intervals[0][0])) / 3600) if intervals else 1
        result['forecast'] = forecast(s['soc'], s['load_w'], s['pv_w'], c, hours)
        if intervals: result['forecast']['restore_at'] = intervals[0][1]
    return result

# ---------------------------------------------------------------- telegram

TG_OUTBOX = queue.Queue(maxsize=200)
PAIRING = {}
BOT_INFO = {}

def tg(method, data, token=None, timeout=35):
    token = token or config()['telegram_token'] or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token: raise ValueError('Telegram-бот не налаштований')
    req = Request('https://api.telegram.org/bot' + token + '/' + method, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=timeout) as f: response = json.load(f)
    if not response.get('ok'): raise OSError('Telegram API error')
    return response['result']

def bot_token():
    return config()['telegram_token'] or os.environ.get('TELEGRAM_BOT_TOKEN', '')

def notify(message):
    """Queue a message for all paired users. Never blocks the inverter polling loop."""
    if not bot_token(): return
    try: TG_OUTBOX.put_nowait(message)
    except queue.Full: pass

def telegram_sender():
    while True:
        message = TG_OUTBOX.get()
        for uid in config()['telegram_users']:
            for attempt in range(3):
                try: tg('sendMessage', dict(chat_id=uid, text=message[:4000])); break
                except Exception: time.sleep(3 * (attempt + 1))

def set_telegram_token(token):
    token = str(token or '').strip()
    if not token:
        update_config(telegram_token=''); BOT_INFO.clear(); return dict(ok=True, enabled=False)
    if not (5 < len(token) < 100 and ':' in token): raise ValueError('Невірний формат токена від @BotFather')
    me = tg('getMe', {}, token=token, timeout=10)
    update_config(telegram_token=token); BOT_INFO.clear(); BOT_INFO.update(me)
    return dict(ok=True, enabled=True, username=me.get('username'))

def telegram_info():
    c = config()
    token = bot_token()
    if token and not BOT_INFO:
        try: BOT_INFO.update(tg('getMe', {}, token=token, timeout=10))
        except Exception: pass
    return dict(enabled=bool(token), username=BOT_INFO.get('username'), users=c['telegram_users'])

def start_pairing():
    if not bot_token(): raise ValueError('Спочатку збережіть токен бота')
    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
    PAIRING.clear(); PAIRING[code] = time.time() + 600
    info = telegram_info(); link = ('https://t.me/' + info['username'] + '?start=' + code) if info.get('username') else None
    return dict(code=code, expires_in=600, link=link)

def remove_telegram_user(uid):
    with LOCK:
        c = config(); c['telegram_users'] = [x for x in c['telegram_users'] if x != int(uid)]; save(c)
    return dict(ok=True)

def summary():
    s = status(); c = config()
    if not s['online']: return 'Немає актуального зв’язку з інвертором\n' + schedule_summary(s)
    p = s['sample']; f = s.get('forecast', {})
    lines = [('ДЕМО · ' if s['demo'] else '') + (s['site_name'] or 'Мій дім'),
             f"🔋 SOC: {p['soc']:.0f}%" + (f" · {p['battery_w']:+.0f} Вт" if 'battery_w' in p else ''),
             f"☀️ Сонце: {p['pv_w']:.0f} Вт", f"🏠 Навантаження: {p['load_w']:.0f} Вт",
             f"🔌 Мережа: {'є' if p['grid_on'] else 'немає'}" + (f" · імпорт {p['grid_import_w']:.0f} Вт" if p.get('grid_import_w') else '') + (f" · експорт {p['grid_export_w']:.0f} Вт" if p.get('grid_export_w') else '')]
    if 'today_pv_kwh' in p: lines.append(f"Сьогодні: PV {p['today_pv_kwh']:.1f} · спожито {p.get('today_load_kwh',0):.1f} · імпорт {p.get('today_import_kwh',0):.1f} кВт·год")
    if f.get('restore_at'):
        lines.append(f"Заряд на момент ввімкнення світла ({clock(f['restore_at'], c)}): ≈{f['projected_soc']:.0f}%" + (' ⚠ може не вистачити' if f['insufficient'] else ''))
    elif f.get('hours_to_reserve') is not None:
        lines.append(f"До резерву {c['reserve_soc']:.0f}%: ≈{f['hours_to_reserve']:.1f} год")
    return '\n'.join(lines) + '\n' + schedule_summary(s)

def report(hours=24):
    hours = float(hours)
    if not math.isfinite(hours) or not 0 < hours <= 2160: raise ValueError('Період звіту: 0–2160 год')
    now = time.time(); c = config()
    with database() as d: rows = d.execute('SELECT t,payload FROM samples WHERE t>=? AND t<=? ORDER BY t', (now-hours*3600,now)).fetchall()
    mode_demo = c['mode'] == 'demo'
    site_id = c['active_site']
    samples = []
    for t, payload in rows:
        point = json.loads(payload)
        if bool(point.get('demo')) == mode_demo and point.get('site_id','default') == site_id: samples.append((t, point))
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
    # Hour buckets keep averages without bridging missing measurements.
    buckets = {}
    for t,p in samples: buckets.setdefault(int(t//3600)*3600,[]).append(p)
    series=[]
    for t,group in sorted(buckets.items()):
        point={'t':t,'demo':mode_demo}
        for key in (*keys,'battery_w','soc'):
            values=[x[key] for x in group if key in x]
            point[key]=sum(values)/len(values) if values else None
        series.append(point)
    today = None
    if samples and 'today_pv_kwh' in samples[-1][1]:
        last = samples[-1][1]; today = {k: last[k] for k in last if k.startswith('today_')}
    return dict(samples=len(samples), hours=hours, demo=mode_demo, coverage_hours=covered/3600, inverter_today=today,
                metric_coverage_hours={k:v/3600 for k,v in metric_seconds.items()}, series=series,
                **{k:round(v,4) if v is not None else None for k,v in totals.items()},
                note='Інтеграція потужностей, пропуски понад 120 с виключені. Відсутній канал не замінюється оцінкою. Покриття різних каналів може відрізнятися.')

def capabilities():
    c=config()
    writable={}
    if c['mode'] in ('modbus_tcp','solarman_v5') and c['verified_profile'] and c['allow_writes']: writable=setting_definitions(c)
    return dict(version=APP_VERSION,protocol='deye-control-v5',writable=writable,settings=setting_definitions(c),metrics=list(telemetry_channels(c)),demo=c['mode']=='demo')

def report_message(hours=24):
    r = report(hours); c = config()
    def v(key): return '—' if r.get(key) is None else f"{r[key]:.2f}"
    lines = [f"📊 Звіт за {hours:.0f} год · {c['address'] or 'Мій дім'}" + (' (ДЕМО)' if r['demo'] else ''),
             f"Генерація: {v('pv_kwh')} кВт·год", f"Споживання: {v('load_kwh')} кВт·год",
             f"Імпорт: {v('grid_import_kwh')} · Експорт: {v('grid_export_kwh')} кВт·год",
             f"Батарея: заряд {v('battery_charge_kwh')} · розряд {v('battery_discharge_kwh')} кВт·год",
             f"Покриття вимірюваннями: {r['coverage_hours']:.1f} з {hours:.0f} год"]
    if r.get('inverter_today'):
        t = r['inverter_today']
        lines.append('Лічильники інвертора сьогодні: PV ' + str(t.get('today_pv_kwh')) + ', спожито ' + str(t.get('today_load_kwh')) + ', імпорт ' + str(t.get('today_import_kwh')) + ', експорт ' + str(t.get('today_export_kwh')) + ' кВт·год')
    return '\n'.join(lines)

def settings_message():
    c = config(); defs = setting_definitions(c)
    if not defs: return 'У профілі немає параметрів для зміни.'
    try: values = read_settings()
    except Exception as e: values = {}; err = str(e)
    else: err = None
    lines = ['⚙️ Параметри інвертора' + ('' if c['allow_writes'] and c['verified_profile'] else ' (зміна заблокована в панелі)')]
    for name, d in defs.items():
        val = values.get(name)
        if d.get('kind') == 'clock': lines.append(f"{name}: {val or '—'}  ({d.get('label','')})"); continue
        shown = '—' if val is None else (d.get('options', {}).get(str(int(val)), str(val)) if 'options' in d else (f"{int(val):04d}" if d.get('format') == 'hhmm' else f"{val:g}"))
        lines.append(f"{name}: {shown} {d.get('unit','')}  ({d.get('label','')})")
    if err: lines.append('Не вдалося прочитати: ' + err)
    lines.append('Зміна: /set назва значення')
    return '\n'.join(lines)

HELP = ('/status — показники зараз\n/report — звіт за 24 год\n/week — звіт за 7 днів\n/schedule — найближче відключення\n'
        '/settings — параметри інвертора\n/set назва значення — змінити параметр\n/confirm код — підтвердити зміну\n/id — ваш Telegram ID')

def handle_bot_message(m, pending):
    uid = m.get('from', {}).get('id'); chat = m.get('chat', {})
    if chat.get('type') != 'private' or not isinstance(uid, int): return None
    parts = m.get('text','').split(); cmd = parts[0].split('@')[0].lower() if parts else ''
    users = config()['telegram_users']
    if cmd in ('/start', '/pair') and len(parts) == 2:
        code = parts[1].upper(); expires = PAIRING.get(code)
        if expires and expires > time.time():
            PAIRING.pop(code, None)
            with LOCK:
                c = config(); c['telegram_users'] = [*c['telegram_users'], uid]; save(c)
            return '✅ Доступ надано. Ви отримуватимете сповіщення про мережу, батарею й відключення.\n\n' + HELP
        return 'Код недійсний або прострочений. Створіть новий у панелі: «Зв’язок» → Telegram.'
    if uid not in users:
        if cmd in ('/start', '/id'): return f'Ваш Telegram ID: {uid}\nЩоб отримати доступ, створіть код у панелі DEYE («Зв’язок» → Telegram) і надішліть /pair КОД.'
        return None
    if cmd == '/status': return summary()
    if cmd == '/report': return report_message(24)
    if cmd == '/week': return report_message(168)
    if cmd == '/schedule': return schedule_summary()
    if cmd == '/settings': return settings_message()
    if cmd == '/id': return f'Ваш Telegram ID: {uid}'
    if cmd == '/set' and len(parts) == 3:
        c = config(); r = c['registers'].get(parts[1])
        if not r or not r.get('writable') or r.get('kind') == 'clock': raise ValueError('Невідомий параметр. Список: /settings')
        if r.get('risk') == 'danger': raise ValueError('Цей параметр небезпечний і змінюється лише в панелі з окремим підтвердженням.')
        value = check_value(parts[1], r, float(parts[2].replace(',', '.')), c)
        nonce = secrets.token_hex(3); pending[uid] = (nonce, time.time()+60, parts[1], value)
        label = r.get('options', {}).get(str(int(value)), parts[2]) if 'options' in r else parts[2]
        return f"Зміна «{r.get('label', parts[1])}» → {label} {r.get('unit','')}.\nПідтвердьте протягом 60 с: /confirm {nonce}"
    if cmd == '/confirm' and len(parts) == 2:
        p = pending.pop(uid, None)
        if not p or p[0] != parts[1] or p[1] < time.time(): raise ValueError('Підтвердження недійсне')
        return '✅ Записано й перевірено читанням: ' + str(write_setting(p[2], p[3], 'telegram:' + str(uid)))
    return HELP

def bot():
    offset = 0; pending = {}; registered = None
    while True:
        token = bot_token()
        if not token: time.sleep(10); continue
        try:
            if registered != token:
                tg('setMyCommands', dict(commands=[dict(command=c, description=d) for c, d in (('status','Показники зараз'),('report','Звіт за 24 год'),('week','Звіт за 7 днів'),('schedule','Графік відключень'),('settings','Параметри інвертора'),('id','Мій Telegram ID'))]), token=token, timeout=10)
                registered = token
            for update in tg('getUpdates', dict(offset=offset, timeout=25, allowed_updates=['message']), token=token):
                offset = update['update_id']+1; m = update.get('message', {})
                try: reply = handle_bot_message(m, pending)
                except Exception as e: reply = '⚠ ' + str(e)
                if reply: tg('sendMessage', dict(chat_id=m['chat']['id'], text=reply[:4000]), token=token)
        except Exception: time.sleep(5)

# ---------------------------------------------------------------- polling

def fetch_schedule(c):
    if not c['schedule_url'].startswith('https://'): raise ValueError('Джерело має бути HTTPS')
    with urlopen(c['schedule_url'], timeout=10) as f: events=json.loads(f.read(1000000))
    if not isinstance(events, list): raise ValueError('Графік має бути JSON-масивом')
    return update_config(outages=events)

def poll():
    previous = None; previous_site = None; was_online = None; warned = set(); last_schedule=0; schedule_ok=True; last_daily=None; last_cleanup=0
    while True:
        started = time.monotonic()
        c = config()
        if previous_site != c['active_site']:
            previous = None; was_online = None; previous_site = c['active_site']; warned = set()
        if c['schedule_url'] and time.time()-last_schedule>300:
            last_schedule=time.time()
            try:
                c=fetch_schedule(c)
                if not schedule_ok: notify('Графік відключень знову оновлюється')
                schedule_ok=True
            except Exception:
                if schedule_ok: notify('Не вдалося оновити графік відключень. Використовується збережений, він може бути застарілим.')
                schedule_ok=False
        try:
            if c['mode']=='demo': s=dict(soc=72.,pv_w=1400.,load_w=800.,grid_on=True,grid_import_w=0.,grid_export_w=100.,solar_direct_w=800.,battery_w=500.)
            else: s=read_sample(c)
            s.setdefault('t',time.time())
            s.update(demo=c['mode']=='demo',site_id=c['active_site'])
            if config()['active_site'] != c['active_site']: continue
            with STATE_LOCK:
                STATE.update(online=True,demo=s['demo'],error=None,sample=s)
                SITE_STATES[c['active_site']] = dict(STATE)
            with database() as d:
                d.execute('INSERT INTO samples VALUES(?,?)',(s['t'],json.dumps(s)))
                if time.time()-last_cleanup > 3600:
                    d.execute('DELETE FROM samples WHERE t<?',(time.time()-90*86400,)); last_cleanup=time.time()
            if not s['demo']:
                if was_online is False: notify('✅ Зв’язок з інвертором відновлено\n'+summary())
                if previous is not None and previous != s['grid_on']: notify(('💡 Світло з’явилося\n' if s['grid_on'] else '🔌 Світло зникло\n')+summary())
                previous=s['grid_on']
                st=status(); o=st['outage']; f=st.get('forecast') or {}
                if o:
                    key=(o['start'],o['end'],o['group']); delta=stamp(o['start'])-time.time()
                    if 0<delta<=float(c['warning_minutes'])*60 and key not in warned:
                        notify(f"⏰ Через {delta/60:.0f} хв планове відключення ({clock(stamp(o['start']),c)}–{clock(stamp(o['end']),c)}, група {o['group']})\n"+summary()); warned.add(key)
                    if f.get('insufficient') and ('low',key) not in warned:
                        notify('⚠ За поточним навантаженням заряду може не вистачити до планового ввімкнення світла.\n'+summary()); warned.add(('low',key))
                if s['soc'] <= float(c['reserve_soc']) and 'reserve' not in warned:
                    notify(f"🪫 Заряд досяг резерву {c['reserve_soc']:.0f}%\n"+summary()); warned.add('reserve')
                if s['soc'] > float(c['reserve_soc']) + 5: warned.discard('reserve')
                now_local=local_now(c); day=now_local.date().isoformat()
                if int(c['daily_report_hour']) >= 0 and now_local.hour==int(c['daily_report_hour']) and last_daily!=day:
                    notify(report_message(24)); last_daily=day
            was_online=True
        except Exception as e:
            log('poll failed:', type(e).__name__, e)
            with STATE_LOCK:
                STATE.update(online=False,error=str(e) or type(e).__name__)
                SITE_STATES[c['active_site']] = dict(STATE)
            if was_online is True: notify('⚠ Втрачено зв’язок з інвертором; прогноз призупинено')
            was_online=False
        time.sleep(max(.2, float(c['poll_seconds'])-(time.monotonic()-started)))

def background_poll():
    previous = {}
    def collect(site):
        identity=site['id']
        try:
            if site['mode'] not in ('modbus_tcp','solarman_v5'): return
            point=read_sample(DEFAULT|site)
            point.setdefault('t',time.time())
            point.update(demo=False,site_id=identity)
            with STATE_LOCK: SITE_STATES[identity]=dict(online=True,demo=False,error=None,sample=point)
            with database() as d: d.execute('INSERT INTO samples VALUES(?,?)',(point['t'],json.dumps(point)))
            old=previous.get(identity)
            if old is not None and old != point['grid_on']: notify(('💡 Світло з’явилося' if point['grid_on'] else '🔌 Світло зникло')+' · '+site.get('address',identity)+' · SOC '+f"{point['soc']:.0f}"+'%')
            previous[identity]=point['grid_on']
        except Exception as e:
            with STATE_LOCK: SITE_STATES[identity]=dict(online=False,demo=False,error=str(e) or type(e).__name__,sample=SITE_STATES.get(identity,{}).get('sample'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            started=time.monotonic();c=config()
            profiles=[site for site in c['sites'] if site['id']!=c['active_site']]
            list(pool.map(collect,profiles))
            time.sleep(max(.2,float(c['poll_seconds'])-(time.monotonic()-started)))

# ---------------------------------------------------------------- households

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
    if not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10')): raise ValueError('Для знайдених інверторів потрібна локальна або VPN IPv4 адреса')
    with LOCK:
        c=config()
        if len(c['sites']) >= 64: raise ValueError('До 64 домогосподарств на сервер')
        port=int(body.get('port',502))
        site={k:DEFAULT[k] for k in SITE_FIELDS};site.update(id=secrets.token_hex(6),host=host,port=port,address=str(body.get('name') or host)[:160],mode='solarman_v5' if port==8899 else 'modbus_tcp',registers={},verified_profile=False,allow_writes=False)
        if body.get('logger_serial'): site['logger_serial']=int(body['logger_serial'])
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

# ---------------------------------------------------------------- HTTP

def load_password():
    """DEYE_PASSWORD, else a persistent random password in the private data folder (for autostart)."""
    if os.environ.get('DEYE_PASSWORD'): return os.environ['DEYE_PASSWORD']
    p = DATA / 'server-password.txt'
    if not p.exists():
        p.write_text(secrets.token_urlsafe(24), encoding='utf-8')
        try: p.chmod(0o600)
        except OSError: pass
    return p.read_text(encoding='utf-8').strip()

PASSWORD=os.environ.get('DEYE_PASSWORD','')
LOCAL_SESSION_TOKEN=secrets.token_urlsafe(32)
FAILURES = defaultdict(deque)

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # A browser may cancel static fetches or close a stream during navigation.
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
    def send(self, code, body, kind='application/json', extra_headers=None):
        data=body.encode() if isinstance(body,str) else body
        self.send_response(code)
        for key,value in (extra_headers or {}).items(): self.send_header(key,value)
        self.send_header('Content-Type',kind+'; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def json(self, value, code=200):
        return self.send(code, json.dumps(value, ensure_ascii=False))
    def client_id(self):
        ip=self.client_address[0]
        forwarded=(self.headers.get('X-Forwarded-For') or '') if ip in ('127.0.0.1','::1') else ''
        return ip+'|'+forwarded.split(',')[0].strip()
    def auth(self):
        local=self.client_address[0] in ('127.0.0.1','::1')
        cookies=SimpleCookie()
        try: cookies.load(self.headers.get('Cookie',''))
        except Exception: pass
        session=cookies.get('deye_local_session')
        if local and session and hmac.compare_digest(session.value,LOCAL_SESSION_TOKEN): return True
        who=self.client_id(); now=time.time(); history=FAILURES[who]
        while history and history[0] < now-600: history.popleft()
        if len(history) >= 10:
            self.send(429,json.dumps({'error':'Забагато невдалих спроб входу. Зачекайте 10 хвилин.'},ensure_ascii=False)); return False
        expected='Basic '+base64.b64encode(('admin:'+PASSWORD).encode()).decode()
        given=self.headers.get('Authorization','')
        if PASSWORD and hmac.compare_digest(given.encode(),expected.encode()): return True
        if given: history.append(now)
        self.send(401,json.dumps({'error':'Потрібне повторне підключення: відкрийте панель через ярлик DEYE або введіть пароль сервера.'},ensure_ascii=False))
        return False
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
        if self.path in ('/', '/index.html'): return self.send(200,(STATIC/'index.html').read_bytes(),'text/html')
        if self.path=='/api/health': return self.json(dict(ok=True,version=APP_VERSION))
        if not self.auth(): return
        try:
            if self.path=='/api/stream':
                self.send_response(200); self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.end_headers()
                try:
                    last=None
                    for _ in range(300):
                        payload=json.dumps(status(),ensure_ascii=False)
                        # Only push when something changed; a comment line keeps proxies from closing the stream.
                        self.wfile.write(('data: '+payload+'\n\n' if payload!=last else ': keepalive\n\n').encode());self.wfile.flush();last=payload;time.sleep(2)
                except (BrokenPipeError,ConnectionResetError,OSError): pass
                return
            if self.path=='/api/sites': return self.json(site_list())
            if self.path=='/api/config': return self.json(public_config())
            if self.path=='/api/status': return self.json(status())
            if self.path=='/api/report': return self.json(report(query.get('hours',[24])[0]))
            if self.path=='/api/capabilities': return self.json(capabilities())
            if self.path=='/api/settings': return self.json(settings_state())
            if self.path=='/api/telegram': return self.json(telegram_info())
            if self.path=='/api/audit':
                with database() as d: rows=d.execute('SELECT t,actor,setting,old,new FROM audit ORDER BY t DESC LIMIT 100').fetchall()
                return self.json([dict(zip(('t','actor','setting','old','new'),row)) for row in rows])
            if self.path=='/api/history.csv':
                out=io.StringIO(); w=csv.writer(out); w.writerow(['UTC','SOC','PV W','Load W','Grid','Demo','Import W','Export W','Direct solar W','Battery W'])
                with database() as d:
                    for t,p in d.execute('SELECT t,payload FROM samples ORDER BY t'):
                        s=json.loads(p); w.writerow([datetime.fromtimestamp(t,timezone.utc).isoformat(),s.get('soc'),s.get('pv_w'),s.get('load_w'),s.get('grid_on'),s.get('demo'),s.get('grid_import_w'),s.get('grid_export_w'),s.get('solar_direct_w'),s.get('battery_w')])
                return self.send(200,out.getvalue(),'text/csv')
        except ValueError as e: return self.json({'error':str(e)},400)
        except Exception as e: return self.json({'error':type(e).__name__+': '+str(e)},502)
        self.send(404,'{}')
    def do_POST(self):
        if not self.auth(): return
        # JSON-only + custom header prevents cross-origin form submissions.
        if self.headers.get('X-Deye-Request')!='1' or self.headers.get('Content-Type')!='application/json': return self.send(403,'{}')
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=1000000: raise ValueError('Розмір запиту')
            body=json.loads(self.rfile.read(n))
            if self.path in ('/api/session','/api/session/logout'):
                if self.client_address[0] not in ('127.0.0.1','::1'): raise ValueError('Локальна сесія доступна лише через loopback')
                active=self.path=='/api/session'
                cookie='deye_local_session='+ (LOCAL_SESSION_TOKEN if active else '')+'; HttpOnly; SameSite=Strict; Path=/api; Max-Age='+('2592000' if active else '0')
                return self.send(200,json.dumps({'ok':True}),extra_headers={'Set-Cookie':cookie})
            if self.path=='/api/config':
                if set(body)-set(DEFAULT) or set(body)&set(SECRET_FIELDS): raise ValueError('Невідомі поля')
                with LOCK: save(config() | body)
                result={'ok':True}
            elif self.path=='/api/settings/read': result=start_settings_read()
            elif self.path=='/api/profile': result=apply_profile(body['name'])
            elif self.path=='/api/telegram': result=set_telegram_token(body.get('token'))
            elif self.path=='/api/telegram/pair': result=start_pairing()
            elif self.path=='/api/telegram/remove': result=remove_telegram_user(body['id'])
            elif self.path=='/api/telegram/test':
                if not config()['telegram_users']: raise ValueError('Немає підключених користувачів Telegram')
                notify('🔔 Тестове повідомлення DEYE Control\n'+summary()); result={'ok':True}
            elif self.path=='/api/discover': result=discover_network(body['cidr'])
            elif self.path=='/api/sites': result=add_site(body)
            elif self.path=='/api/site/select': result=select_site(body['site_id'])
            elif self.path=='/api/write':
                if body.get('confirm') is not True: raise ValueError('Потрібне підтвердження')
                result={'value':write_setting(body['name'],body['value'],'web',danger_ok=body.get('danger_confirm')=='ТАК')}
            elif self.path=='/api/clock':
                if body.get('confirm') is not True: raise ValueError('Потрібне підтвердження')
                result={'value':sync_clock('web')}
            else: return self.send(404,'{}')
            self.json(result)
        except Exception as e: self.json({'error':str(e)},400)

def load_session_token():
    """Persistent so a phone stays signed in across server restarts."""
    p = DATA / 'session-token.txt'
    if not p.exists():
        p.write_text(secrets.token_urlsafe(32), encoding='utf-8')
        try: p.chmod(0o600)
        except OSError: pass
    return p.read_text(encoding='utf-8').strip()

def main():
    global PASSWORD, LOCAL_SESSION_TOKEN
    PASSWORD = load_password()
    LOCAL_SESSION_TOKEN = load_session_token()
    if len(PASSWORD)<12: raise SystemExit('Задайте DEYE_PASSWORD довжиною щонайменше 12 символів')
    db().close()
    ThreadingHTTPServer.daemon_threads = True
    # Bind first: if the port is busy nothing else starts, so a restarting launcher never duplicates pollers.
    server = ThreadingHTTPServer((os.environ.get('DEYE_BIND','127.0.0.1'),int(os.environ.get('DEYE_PORT','8080'))),Handler)
    for target in (poll, background_poll, bot, telegram_sender):
        threading.Thread(target=target,daemon=True).start()
    server.serve_forever()
if __name__=='__main__': main()
