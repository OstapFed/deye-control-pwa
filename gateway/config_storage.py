"""Read current UTF-8 config and the legacy Windows-1251 config format."""
import json

def read_config(path):
    raw=path.read_bytes()
    try:text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:text=raw.decode('cp1251')
    value=json.loads(text)
    if not isinstance(value,dict):raise ValueError('Config must be a JSON object')
    return value
