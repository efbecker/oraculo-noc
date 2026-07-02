with open('/opt/oraculo-noc/config.py', 'r') as f:
    lines = f.readlines()
with open('/opt/oraculo-noc/config.py', 'w') as f:
    for line in lines:
        if line.startswith('DB_PATH'):
            f.write("DB_PATH = '/opt/oraculo-noc/oraculo_zabbix.db'\n")
        else:
            f.write(line)
