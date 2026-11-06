"""Read-only Solarman V5 probe. Does not write registers or modify configuration."""
import argparse,json,sys
from solarman_client import create_v5_client

def main():
    parser=argparse.ArgumentParser(description='Перевірка читання Solarman V5 без запису налаштувань')
    parser.add_argument('--host',required=True)
    parser.add_argument('--serial',type=int,required=True,help='SN логера, не інвертора')
    parser.add_argument('--port',type=int,default=8899)
    parser.add_argument('--unit',type=int,default=1)
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--count',type=int,default=10)
    args=parser.parse_args()
    if not 0<args.serial<=0xffffffff or not 0<=args.start<=65535 or not 1<=args.count<=40 or args.start+args.count>65536:parser.error('Перевірте SN, адресу та кількість (1–40)')
    try:import pysolarmanv5
    except ImportError:raise SystemExit('Спочатку: py -m pip install -r gateway/requirements.txt')
    client=None
    try:
        client=create_v5_client(args.host,args.serial,port=args.port,mb_slave_id=args.unit,socket_timeout=6,auto_reconnect=False,verbose=False)
        data=client.read_holding_registers(args.start,args.count)
        print('Solarman V5: отримано відповідь на читання. Налаштування не змінювалися.')
        print(json.dumps({str(args.start+i):value for i,value in enumerate(data)},indent=2))
        print('Це сирі регістри. Модель і значення потрібно звірити з картою Deye.')
    except Exception as e:
        print('Не вдалося прочитати регістри: '+str(e),file=sys.stderr)
        print('Перевірте IP, SN логера, Modbus Unit ID та підтримку V5. Відкритий порт не гарантує відповіді.',file=sys.stderr)
        return 1
    finally:
        if client:client.disconnect()
    return 0
if __name__=='__main__':sys.exit(main())
