"""Build a Deye profile (telemetry + every writable setting) from an ha-solarman definition.

usage (three-phase): gen_profile.py deye_p3.yaml gateway/profiles/deye_sg04lp3.json
usage (single-phase): gen_profile.py deye_hybrid.yaml gateway/profiles/deye_sg0xlp1.json --telemetry tools/telemetry_sg0xlp1.json --max-amps 190 --max-watts 8000 --power-reg 43
usage: gen_profile.py SOURCE.yaml OUT.json [--telemetry BASE.json] [--max-amps 240] [--max-watts 12000] [--clock-reg N] [--power-reg N]
Telemetry channels are kept from OUT.json (or taken from --telemetry); settings are regenerated.
"""
import yaml,json,re,sys,argparse
ap=argparse.ArgumentParser();ap.add_argument('src');ap.add_argument('dst');ap.add_argument('--telemetry');ap.add_argument('--max-amps',type=float,default=240);ap.add_argument('--max-watts',type=float,default=12000);ap.add_argument('--power-reg',type=int,default=80)
A=ap.parse_args();SRC=A.src;DST=A.dst
d=yaml.safe_load(open(SRC))
UA={
'Self-check time':'Час самоперевірки','Battery Control Mode':'Тип батареї (керування)','Battery Equalization':'Напруга вирівнювання','Battery Absorption':'Напруга абсорбції','Battery Float':'Напруга підтримки (Float)','Battery Capacity':'Ємність батареї','Battery Empty':'Напруга «порожньої» батареї','Zero Export power':'Потужність нульового експорту','Battery Equalization Cycle':'Цикл вирівнювання','Battery Equalization Time':'Тривалість вирівнювання','Battery Temperature Compensation':'Температурна компенсація','Battery Max Charging Current':'Макс. струм заряду батареї','Battery Max Discharging Current':'Макс. струм розряду батареї','Battery Operation Mode':'Режим роботи батареї (за напругою/SOC)','Battery Wake Up':'Пробудження батареї','Battery Resistance':'Внутрішній опір батареї','Battery Charging efficiency':'ККД заряду батареї','Battery Shutdown SOC':'SOC вимкнення (Shutdown)','Battery Restart SOC':'SOC перезапуску (Restart)','Battery Low SOC':'Низький SOC (Low Batt)','Battery Shutdown Voltage':'Напруга вимкнення (Shutdown)','Battery Restart Voltage':'Напруга перезапуску (Restart)','Battery Low Voltage':'Низька напруга (Low Batt)','Generator Operating Time':'Час роботи генератора','Generator Cooling Time':'Час охолодження генератора','Battery Generator Charging Start Voltage':'Заряд від генератора: старт за напругою','Battery Generator Charging Start':'Заряд від генератора: старт при SOC','Battery Generator Charging Current':'Струм заряду від генератора','Battery Grid Charging Start Voltage':'Заряд від мережі: старт за напругою','Battery Grid Charging Start':'Заряд від мережі: старт при SOC','Battery Grid Charging Current':'Струм заряду від мережі','Battery Generator Charging':'Заряд від генератора','Battery Grid Charging':'Заряд від мережі','Generator':'Генератор','IO Mode':'Режим входу GEN (генератор/SmartLoad)','SmartLoad Off Voltage':'SmartLoad: вимкнути при напрузі','SmartLoad Off':'SmartLoad: вимкнути при SOC','SmartLoad On Voltage':'SmartLoad: увімкнути при напрузі','SmartLoad On':'SmartLoad: увімкнути при SOC','Grid Voltage':'Номінальна напруга мережі','Generator Min PV Power':'Мін. PV-потужність для SmartLoad','Charging Signal':'Сигнал заряду','Energy Pattern':'Пріоритет енергії (батарея/навантаження)','Work Mode':'Режим роботи','Export Surplus Power':'Макс. потужність продажу','Export Surplus':'Продаж сонячної енергії','Export Asymmetry':'Асиметричний експорт по фазах','PV Power':'Макс. потужність PV','MPPT multi-point scanning':'MPPT багатоточкове сканування','Time of Use':'Розклад (Time of Use)','Microinverter Export cut-off':'Відсічення експорту мікроінвертора','Generator Peak shaving':'Peak shaving генератора','Grid Peak shaving':'Peak shaving мережі','Generator On-Grid':'Генератор паралельно з мережею','Off Grid':'Автономний режим (Off Grid)','Grid reconnection time':'Затримка повторного підключення до мережі','Grid frequency':'Номінальна частота мережі','Grid voltage protection - high':'Захист мережі: макс. напруга','Grid voltage protection - low':'Захист мережі: мін. напруга','Grid frequency protection - high':'Захист мережі: макс. частота','Grid frequency protection - low':'Захист мережі: мін. частота','Generator Grid side':'Генератор на стороні мережі','Grid Max Export power':'Макс. потужність експорту в мережу','Grid Max Import power':'Макс. потужність імпорту з мережі','AC Coupling':'AC Coupling','Inverter Power':'Інвертор увімкнено (ON/OFF)','Battery BMS Type':'Тип BMS / протокол',
}
GROUPS={'Control':'Сервіс','Settings':'Батарея','Grid Parameters':'Мережа','Work Mode':'Режим','BMS':'Батарея'}
# Parameters whose wrong value can damage batteries, trip protections or break grid compliance.
DANGER=re.compile(r'Voltage|Equalization|Absorption|Float|Empty|Temperature Compensation|Resistance|Control Mode|Operation Mode|BMS Type|protection|Grid frequency$|Grid Voltage$|Off Grid|Wake Up|Self-check|Capacity|Charging efficiency|AC Coupling|IO Mode|Charging Signal|Generator Grid side|Generator On-Grid|Microinverter|Inverter Power')
def first(v): return v[0] if isinstance(v,list) else v
def slug(name,reg,used):
    s=re.sub(r'[^a-z0-9]+','_',name.lower()).strip('_') or 'reg'
    if s in used: s+='_'+str(reg)
    used.add(s);return s
OPT_UA={'Disabled':'Вимкнено','Enabled':'Увімкнено','Export First':'Продаж надлишку (Export First)','Zero Export To Load':'Нуль експорту на навантаження','Zero Export To CT':'Нуль експорту на CT','Grid':'Мережа','Generator':'Генератор','Both':'Мережа і генератор','Voltage':'За напругою','Capacity':'За SOC (%)','No Battery':'Без батареї','Lithium':'Літієва (BMS)','Battery First':'Спершу батарея','Load First':'Спершу навантаження','Week':'Щодня','Weekdays':'Будні','Weekend':'Вихідні'}
out={}
used=set()
for g in d['parameters']:
  for it in g['items']:
    p=it.get('platform')
    if p not in ('number','select','switch','time','datetime'): continue
    if it['registers'][0]==A.power_reg and not it.get('name'): it=dict(it,name='Inverter Power')
    if not it.get('name'): continue
    if it['name']=='Battery Wake Up': continue  # shares register 129 with generator charging; momentary action
    if it['name']=='Meter' and 'value' not in it: continue  # whole-register switch on a bit-field register: unsafe
    if it['name']=='AC Coupling' and any('bit' in o for o in it.get('lookup',[])):
        # Bit flags in a shared register: expose each side as its own bit switch so other flags stay intact.
        for o in it['lookup']:
            if 'bit' not in o or o['value']=='Disabled': continue
            side={'Grid':'на стороні мережі','Load':'на стороні навантаження'}.get(o['value'],o['value'])
            out[slug('ac_coupling_'+o['value'],it['registers'][0],used)]=dict(address=it['registers'][0],bit=o['bit'],writable=True,label='AC Coupling '+side,group='Батарея',risk='danger',options={'0':'Вимкнено','1':'Увімкнено'},min=0,max=1)
        continue
    regs=it['registers'];name=it['name']
    r=dict(address=regs[0],writable=True,label=UA.get(name,name),group=GROUPS.get(g.get('group'),'Інше'))
    if name.startswith('Program'):
        n=name.split()[1];r['label']='Програма '+n+': '+{'Time':'початок','Power':'потужність','Voltage':'напруга','SOC':'цільовий SOC','Charging':'заряд від'}[name.split()[2]];r['group']='Розклад'
    if DANGER.search(name): r['risk']='danger'
    if it.get('rule')==2: r['signed']=True
    scale=first(it.get('scale',1)) or 1
    if scale!=1: r['scale']=scale
    if it.get('uom'): r['unit']={'V':'В','A':'А','W':'Вт','%':'%','s':'с','h':'год','d':'дн','Ah':'А·год','Hz':'Гц','mΩ':'мОм','mV/*C':'мВ/°C'}.get(it['uom'],it['uom'])
    if p=='datetime':
        r=dict(address=regs[0],writable=True,kind='clock',label='Годинник інвертора',group='Сервіс');out['inverter_clock']=r;continue
    if p=='time':
        r.update(min=0,max=2359,format='hhmm',unit='год:хв')
    elif p=='select':
        opts={}
        for o in it.get('lookup',[]):
            if 'bit' in o or isinstance(o.get('key'),list) or o.get('key')=='default': continue
            opts[str(o['key'])]=OPT_UA.get(o['value'],o['value'])
        if name=='Time of Use': opts={'0':'Вимкнено','255':'Увімкнено щодня','63':'Увімкнено: будні','193':'Увімкнено: вихідні'}
        r['options']=opts;ks=[int(k) for k in opts];r.update(min=min(ks),max=max(ks))
    elif p=='switch':
        v=it.get('value')
        if isinstance(v,dict) and 'bit' in v: r['bit']=v['bit'];r['options']={'0':'Вимкнено','1':'Увімкнено'};r.update(min=0,max=1)
        elif isinstance(v,dict) and 'true' in v: r['options']={str(v['false']):'Вимкнено',str(v['true']):'Увімкнено'};r.update(min=min(v['false'],v['true']),max=max(v['false'],v['true']))
        else: r['options']={'0':'Вимкнено','1':'Увімкнено'};r.update(min=0,max=1)
    else:
        rg=it.get('range',{});cf=it.get('configurable',{})
        lo=cf.get('min');hi=cf.get('max')
        lo=first(lo) if lo is not None else (first(rg.get('min',0))*scale)
        hi=first(hi) if hi is not None else (first(rg.get('max',65535))*scale)
        r.update(min=round(lo,4),max=round(hi,4))
    if 'step' in it.get('configurable',{}): r['step']=first(it['configurable']['step'])
    if p=='number' and r.get('unit')=='Вт' and name in ('Generator Peak shaving','Grid Peak shaving'): r['label']+=': потужність'
    out[slug(name,regs[0],used)]=r
# 12 kW LP3 hardware limits (datasheet): currents ≤ 240 A, AC power ≤ 12 kW.
for k,r in out.items():
    if r.get('unit')=='А' and r.get('max',0)>A.max_amps: r['max']=A.max_amps
    if r.get('unit')=='Вт' and r.get('max',0)>A.max_watts: r['max']=A.max_watts
    if k.startswith('program') and r.get('unit')=='Вт': r['max']=A.max_watts
    if r.get('unit')=='В' and r.get('scale')==0.01 and r.get('max',0)>63: r['max']=63
import os
telemetry=json.load(open(A.telemetry if A.telemetry else DST))
telemetry={k:v for k,v in telemetry.items() if not v.get('writable')}
json.dump(telemetry|out,open(DST,'w'),ensure_ascii=False,indent=1)
print(len(out),'settings,',sum(1 for r in out.values() if r.get('risk')),'danger')
for k,r in out.items(): print(k,r['address'],r.get('bit',''),r['label'],r.get('min'),r.get('max'),r.get('scale',''),r.get('unit',''),r.get('risk',''),list(r.get('options',{}).values())[:4])
