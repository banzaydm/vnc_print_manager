# -*- coding: utf-8 -*-
"""Служебные функции сканирования сети: SNMP, ONVIF, SSDP, HTTP, баннеры, ARP."""
import logging
import os
import platform
import re
import selectors
import socket
import ssl
import struct
import subprocess
import time
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger(__name__)

VNC_PORTS = (5900, 5901, 5902, 5903, 5904, 5905, 5800)
RUSTDESK_PORTS = (21115, 21118, 21119)
PRINTER_WEB_PORTS = (80, 443, 9100, 631)
CAMERA_WEB_PORTS = (80, 443, 8080)
ROUTER_WEB_PORTS = (80, 443, 8080, 8443)
RTSP_PORTS = (554, 8554)
SDK_CAMERA_PORTS = (8000, 37777)
ROUTER_TCP_PORTS = (22, 23, 7547)
HOST_PORTS = (135, 139, 445, 3389, 5985)
SYS_WEB_PORTS = (8006, 9090)
DNS_PORT = 53
HTTPS_PORTS = (443, 8006, 8443, 9090, 9443)
WEB_NAME_PORTS = (80, 443, 8006, 8443, 8080, 8888, 9090, 9443)

KEY_PORTS = tuple(sorted(set(VNC_PORTS) | set(RUSTDESK_PORTS) | set(PRINTER_WEB_PORTS)
                         | set(CAMERA_WEB_PORTS) | set(ROUTER_WEB_PORTS)
                         | set(RTSP_PORTS) | set(SDK_CAMERA_PORTS) | set(ROUTER_TCP_PORTS)
                         | set(HOST_PORTS) | set(SYS_WEB_PORTS) | {DNS_PORT}))

_SSL_CTX = ssl._create_unverified_context()

_SNMP_SYSDESCR = '1.3.6.1.2.1.1.1.0'
_SNMP_SYSNAME = '1.3.6.1.2.1.1.5.0'
_SNMP_SYSLOCATION = '1.3.6.1.2.1.1.6.0'
_SNMP_DEFAULT_OIDS = (_SNMP_SYSDESCR, _SNMP_SYSNAME, _SNMP_SYSLOCATION)

_MJPEG_PATHS = (
    '/video/mjpg.jpg', '/mjpg.cgi', '/axis-cgi/mjpg/video.cgi',
    '/cgi-bin/mjpeg', '/video.cgi', '/image.jpg',
)


def tcp_probe_many(ip, ports, timeout=0.8):
    """Параллельная неблокирующая проба TCP-портов. Возвращает список открытых.

    selectors.DefaultSelector (epoll на Linux) — без лимита fd<1024 у select.select.
    """
    pending = {}
    opened = []
    sel = selectors.DefaultSelector()
    try:
        for p in ports:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setblocking(False)
                err = s.connect_ex((str(ip), int(p)))
                if err == 0:
                    opened.append(int(p))
                    s.close()
                    continue
                if err in (10035, 10036, 115, 11, 114, 10037):
                    sel.register(s, selectors.EVENT_WRITE, int(p))
                    pending[s] = int(p)
                    continue
                s.close()
            except Exception:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
        deadline = time.time() + timeout
        while pending:
            remain = deadline - time.time()
            if remain <= 0:
                break
            events = sel.select(remain)
            if not events:
                break
            for key, _mask in events:
                s = key.fileobj
                p = key.data
                try:
                    sel.unregister(s)
                except Exception:
                    pass
                pending.pop(s, None)
                try:
                    err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                except Exception:
                    err = 1
                try:
                    s.close()
                except Exception:
                    pass
                if err == 0:
                    opened.append(p)
    finally:
        for s in list(pending.keys()):
            try:
                sel.unregister(s)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        sel.close()
    return sorted(set(opened))


def _icmp_checksum(data):
    if len(data) % 2:
        data += b'\x00'
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xffff)
    total += total >> 16
    return ~total & 0xffff


def _icmp_ping_raw(ip, timeout):
    """ICMP echo через raw-сокет (нужен root/админ). True — есть echo reply."""
    payload = b'VNCSCAN' + struct.pack('!d', time.time())
    ident = os.getpid() & 0xffff
    header = struct.pack('!BBHHH', 8, 0, 0, ident, 1)
    chk = _icmp_checksum(header + payload)
    packet = struct.pack('!BBHHH', 8, 0, chk, ident, 1) + payload
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (ip, 0))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return False
            sock.settimeout(remain)
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                return False
            if addr[0] != ip:
                continue
            ihl = (data[0] & 0x0f) * 4
            icmp = data[ihl:]
            if len(icmp) < 8 or icmp[0] != 0:
                continue
            if icmp[8:8 + len(payload)] == payload:
                return True
    finally:
        sock.close()


def _icmp_ping_subprocess(ip, timeout):
    """Фоллбэк ICMP — subprocess ping (Linux нет raw-сокета без root)."""
    ms = max(200, int(timeout * 1000))
    if platform.system() == 'Windows':
        cmd = ['ping', '-n', '1', '-w', str(ms), ip]
    else:
        cmd = ['ping', '-c', '1', '-W', str(max(1, int(timeout))), ip]
    try:
        return subprocess.run(cmd, capture_output=True,
                              timeout=timeout + 3).returncode == 0
    except Exception:
        return False


def icmp_ping(ip, timeout=0.8):
    """Жив ли хост по ICMP echo. Raw-сокет, при неудаче — subprocess ping."""
    try:
        return _icmp_ping_raw(ip, timeout)
    except (PermissionError, OSError):
        return _icmp_ping_subprocess(ip, timeout)


def http_probe(ip, port, path='/', timeout=3.0, max_bytes=20000):
    """HTTP(S)-GET без проверки SSL. Возвращает (headers_lower, content, status).

    content — в исходном регистре (для названий); скоринг делает .lower() сам.
    При ошибке — (headers, body, код) для HTTPError и (None, None, None) иначе.
    Схема: HTTPS для HTTPS_PORTS, иначе HTTP; при полном отказе — вторая схема
    (нужно для HTTPS-only сервисов вроде Cockpit на 9090).
    """

    def fetch(scheme):
        url = f'{scheme}://{ip}:{port}{path}'
        try:
            req = urllib.request.Request(url, method='GET',
                                         headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_SSL_CTX) as resp:
                raw = resp.read(max_bytes)
                content = raw.decode('utf-8', errors='ignore')
                headers = {k.lower(): (v or '') for k, v in resp.headers.items()}
                return headers, content, resp.status
        except urllib.error.HTTPError as e:
            try:
                body = e.read(8000).decode('utf-8', errors='ignore')
            except Exception:
                body = ''
            try:
                headers = {k.lower(): (v or '') for k, v in e.headers.items()}
            except Exception:
                headers = {}
            return headers, body, e.code
        except Exception:
            return None

    primary = 'https' if int(port) in HTTPS_PORTS else 'http'
    result = fetch(primary)
    if result is None:
        result = fetch('https' if primary == 'http' else 'http')
    if result is None:
        return None, None, None
    return result


def tcp_banner(ip, port, timeout=2.0, send=None):
    """Чтение TCP-баннера (опционально — предварительная отправка данных)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((str(ip), int(port)))
        if send:
            sock.send(send)
        data = sock.recv(2048)
        sock.close()
        return data.decode('utf-8', errors='ignore')
    except Exception:
        return ''


def rfb_banner(ip, port, timeout=1.5):
    """Баннер VNC: сервер шлёт 'RFB x.xxx\\n' без запроса клиента."""
    return tcp_banner(ip, port, timeout=timeout)


def rtsp_probe(ip, port=554, timeout=2.5):
    """OPTIONS-проба RTSP: возвращает сырой ответ (для Server/WWW-Authenticate)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((str(ip), int(port)))
        sock.send(f'OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode())
        data = sock.recv(4096).decode('utf-8', errors='ignore')
        sock.close()
        return data if 'RTSP/0' in data or 'RTSP/1' in data else ''
    except Exception:
        return ''


def html_title(content):
    """<title> страницы в исходном регистре (до 120 символов)."""
    if not content:
        return None
    m = re.search(r'<title[^>]*>([^<]{2,120})</title>', content, re.IGNORECASE)
    if not m:
        return None
    return ' '.join(m.group(1).split())


SERVER_FINGERPRINTS = (
    ('proxmox', 'Proxmox VE'),
    ('pveproxy', 'Proxmox VE'),
    ('pve-api-daemon', 'Proxmox VE'),
    ('pi-hole', 'Pi-hole'),
    ('pihole', 'Pi-hole'),
    ('vmware esxi', 'VMware ESXi'),
    ('esxi', 'VMware ESXi'),
    ('cockpit', 'Cockpit'),
    ('unraid', 'Unraid'),
    ('synology', 'Synology DSM'),
    ('diskstation', 'Synology DSM'),
    ('truenas', 'TrueNAS'),
    ('openmediavault', 'OpenMediaVault'),
    ('portainer', 'Portainer'),
    ('grafana', 'Grafana'),
    ('home assistant', 'Home Assistant'),
    ('homeassistant', 'Home Assistant'),
    ('qnap', 'QNAP'),
)


def fingerprint_server(headers, content):
    """Название сервиса по title/Server (сильно), затем по телу страницы.

    None — не распознано. Тело проверяется вторым проходом, чтобы случайное
    упоминание продукта в контенте не перебивало реальный title/Server.
    """
    if not headers and not content:
        return None
    title = html_title(content) if content else ''
    server = (headers or {}).get('server', '')
    strong = f'{title or ""} {server}'.lower()
    for key, name in SERVER_FINGERPRINTS:
        if key in strong:
            return name
    body = (content or '')[:20000].lower()
    for key, name in SERVER_FINGERPRINTS:
        if key in body:
            return name
    return None


PRINTER_KEYWORDS = (
    'printer', 'hp', 'canon', 'epson', 'brother', 'xerox',
    'lexmark', 'samsung', 'kyocera', 'ricoh', 'panasonic',
    'sharp', 'toshiba', 'konica', 'minolta', 'oki',
    'dell', 'fuji', 'zebra', 'dymo'
)

ROUTER_SSH_MARKERS = (
    'openwrt', 'dropbear', 'routeros', 'mikrotik', 'cisco', 'd-link', 'dlink',
    'huawei', 'zyxel', 'tp-link', 'tplink', 'edgeos', 'vyos', 'pfsense',
    'fortigate', 'unifi', 'linksys', 'netgear', 'keenetic', 'asuswrt'
)


def looks_like_printer(headers, content):
    """Признаки принтера: Server/Content-Type, ключи только в <title>, '/printer'.

    Ключи в теле страницы не считаем — так роутеры/камеры с упоминанием
    «printer» в меню/JS не попадают в принтеры.
    """
    if not headers and not content:
        return False
    server = (headers or {}).get('server', '').lower()
    ctype = (headers or {}).get('content-type', '').lower()
    if any(k in server for k in PRINTER_KEYWORDS):
        return True
    if any(k in ctype for k in PRINTER_KEYWORDS):
        return True
    if content:
        title = html_title(content)
        if title and any(k in title.lower() for k in PRINTER_KEYWORDS):
            return True
        if '/printer' in content.lower():
            return True
    return False


def ssh_banner_is_router(banner):
    """SSH-баннер указывает на роутер/сетевое устройство (иначе — обычный хост)."""
    if not banner or 'SSH-2.0' not in banner:
        return False
    lower = banner.lower()
    return any(k in lower for k in ROUTER_SSH_MARKERS)


# ---------------------------------------------------------------- SNMP (UDP)

def _b128(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _tlv(tag, payload):
    n = len(payload)
    if n < 0x80:
        ln = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, 'big')
        ln = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + ln + payload


def _int_bytes(n):
    if n == 0:
        return b'\x00'
    raw = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    if raw[0] & 0x80:
        raw = b'\x00' + raw
    return raw


def oid_to_bytes(oid):
    parts = [int(p) for p in str(oid).strip('.').split('.')]
    body = _b128(parts[0] * 40 + parts[1]) if len(parts) > 1 else b''
    for p in parts[2:]:
        body += _b128(p)
    return body


def _oid_to_str(raw):
    if not raw:
        return ''
    vals = []
    cur = 0
    first = True
    for b in raw:
        cur = (cur << 7) | (b & 0x7F)
        if not b & 0x80:
            if first:
                x = min(cur // 40, 2)
                vals.extend([x, cur - 40 * x])
                first = False
            else:
                vals.append(cur)
            cur = 0
    if cur:
        vals.append(cur)
    return '.'.join(str(v) for v in vals)


def snmp_build_request(oids, request_id=1, community='public'):
    varbinds = b''.join(
        _tlv(0x30, _tlv(0x06, oid_to_bytes(o)) + b'\x05\x00') for o in oids
    )
    pdu = _tlv(0xA0, _tlv(0x02, _int_bytes(request_id))
               + _tlv(0x02, b'\x00') + _tlv(0x02, b'\x00')
               + _tlv(0x30, varbinds))
    return _tlv(0x30, _tlv(0x02, b'\x01') + _tlv(0x04, community.encode()) + pdu)


def snmp_parse_response(data):
    """Минимальный BER-разбор GetResponse → {oid_str: value}."""
    pairs = []

    def walk(buf):
        i = 0
        n = len(buf)
        while i < n:
            tag = buf[i]
            i += 1
            if i >= n:
                return
            ln = buf[i]
            i += 1
            if ln & 0x80:
                cnt = ln & 0x7F
                ln = int.from_bytes(buf[i:i + cnt], 'big')
                i += cnt
            val = buf[i:i + ln]
            i += ln
            if tag in (0x30, 0xA0):
                walk(val)
            elif tag == 0x06:
                pairs.append([_oid_to_str(val), None])
            elif tag in (0x04, 0x02) and pairs and pairs[-1][1] is None:
                if tag == 0x04:
                    pairs[-1][1] = val.decode('utf-8', errors='ignore').strip('\x00').strip()
                else:
                    pairs[-1][1] = int.from_bytes(val, 'big') if val else 0

    try:
        walk(data)
    except Exception:
        return {}
    return {oid: value for oid, value in pairs if oid and value is not None}


def snmp_get(ip, oids=None, port=161, timeout=1.0, community='public'):
    """SNMPv2c GET по UDP без внешних утилит. Возвращает {oid: value}."""
    oids = tuple(oids or _SNMP_DEFAULT_OIDS)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(snmp_build_request(oids, community=community), (str(ip), int(port)))
        data, _ = sock.recvfrom(4096)
        sock.close()
    except Exception:
        return {}
    return snmp_parse_response(data)


def snmp_fields(result):
    """{sysDescr, sysName, sysLocation} из результата snmp_get."""
    out = {}
    for oid, value in (result or {}).items():
        if not isinstance(value, str):
            continue
        if oid.endswith('.1.1.1.0'):
            out['sysDescr'] = value
        elif oid.endswith('.1.1.5.0'):
            out['sysName'] = value
        elif oid.endswith('.1.1.6.0'):
            out['sysLocation'] = value
    return out


# ------------------------------------------------------------ ONVIF / SSDP

_ONVIF_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:w="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    '<e:Header><w:MessageID>uuid:{uuid}</w:MessageID>'
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    '</e:Header><e:Body><w:Probe><w:Types>dn:NetworkVideoTransmitter</w:Types></w:Probe></e:Body>'
    '</e:Envelope>'
)


def parse_onvif_probe(text):
    """Разбор ProbeMatch: name/hardware из scopes, XAddrs."""
    if not text or 'ProbeMatch' not in text:
        return None
    out = {}
    scopes = re.findall(r'urn:onvif:(name|hardware|model):([^<"\s]+)', text)
    for key, value in scopes:
        field = 'name' if key == 'name' else 'hardware'
        out.setdefault(field, value)
    m = re.search(r'<[^:>]*:?XAddrs?[^>]*>([^<]+)</', text)
    if m:
        out['xaddr'] = m.group(1).strip().split()[0]
    m = re.search(r'<[^:>]*:?Types?[^>]*>([^<]+)</', text)
    if m and 'NetworkVideoTransmitter' in m.group(1):
        out['onvif'] = True
    return out if (out.get('name') or out.get('hardware') or out.get('xaddr')) else None


def onvif_probe(ip, port=3702, timeout=1.0):
    """ONVIF WS-Discovery (unicast Probe на UDP 3702)."""
    msg = _ONVIF_PROBE.format(uuid=uuid.uuid4()).encode()
    buf = b''
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(msg, (str(ip), int(port)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            buf += data
            if b'ProbeMatch' in buf:
                break
        sock.close()
    except Exception:
        return {}
    return parse_onvif_probe(buf.decode('utf-8', errors='ignore')) or {}


def ssdp_search(ip, st='ssdp:all', timeout=1.2):
    """SSDP M-SEARCH (unicast). Возвращает список ответов (dict ключей заголовков)."""
    msg = (f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
           f'MAN: "ssdp:discover"\r\nMX: 1\r\nST: {st}\r\n\r\n').encode()
    raw = b''
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(msg, (str(ip), 1900))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            raw += data + b'\r\n\r\n'
            if len(raw) > 16000:
                break
        sock.close()
    except Exception:
        return []
    answers = []
    for block in raw.split(b'\r\n\r\n'):
        text = block.decode('utf-8', errors='ignore').strip()
        if 'HTTP/1.1 200' not in text and 'LOCATION' not in text.upper():
            continue
        item = {}
        for line in text.split('\r\n')[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                item[k.strip().lower()] = v.strip()
        if item:
            answers.append(item)
    return answers


def ssdp_device_info(location, timeout=2.0):
    """GET описания UPnP-устройства по LOCATION → friendlyName/modelName/manufacturer."""
    if not location or not location.lower().startswith('http'):
        return {}
    try:
        req = urllib.request.Request(location, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            xml = resp.read(30000).decode('utf-8', errors='ignore')
    except Exception:
        return {}
    info = {}
    for tag, key in (('friendlyName', 'friendly_name'), ('modelName', 'model_name'),
                     ('manufacturer', 'manufacturer'), ('modelNumber', 'model_number')):
        m = re.search(rf'<{tag}[^>]*>([^<]+)</{tag}>', xml, re.IGNORECASE)
        if m:
            info[key] = m.group(1).strip()
    return info


# ---------------------------------------------------- HTTP API камер / MJPEG

def camera_api_probe(ip, port, timeout=2.5):
    """Модель/серийный из встроенных API камер (Hikvision ISAPI, Dahua magicBox)."""
    info = {}
    _, content, status = http_probe(ip, port, '/ISAPI/System/deviceInfo', timeout, 8000)
    if status == 200 and content:
        m = re.search(r'<modelName>([^<]+)</modelName>', content, re.IGNORECASE)
        if m:
            info['model'] = m.group(1).strip()
        m = re.search(r'<serialNumber>([^<]+)</serialNumber>', content, re.IGNORECASE)
        if m:
            info['serial'] = m.group(1).strip()
        m = re.search(r'<manufacturer>([^<]+)</manufacturer>', content, re.IGNORECASE)
        if m:
            info['manufacturer'] = m.group(1).strip()
    if not info.get('model'):
        _, content, status = http_probe(ip, port, '/cgi-bin/magicBox.cgi?method=getDeviceType', timeout, 4000)
        if status == 200 and content and 'deviceType=' in content:
            m = re.search(r'deviceType=([^\s\r\n<]+)', content)
            if m:
                info['model'] = m.group(1).strip()
    if not info.get('serial'):
        _, content, status = http_probe(ip, port, '/cgi-bin/magicBox.cgi?method=getSerialNo', timeout, 4000)
        if status == 200 and content:
            m = re.search(r'(?:sn|serialNo)=([^\s\r\n<]+)', content, re.IGNORECASE)
            if m:
                info['serial'] = m.group(1).strip()
    return info


def mjpeg_probe(ip, port, timeout=1.5):
    """Проверка типичных MJPEG-путей камер. Возвращает путь или None."""
    for path in _MJPEG_PATHS:
        _, _, status = http_probe(ip, port, path, timeout, 4000)
        if status == 200:
            return path
    return None


# ------------------------------------------------------------------- ARP

def get_arp_table():
    """Таблица ARP: {ip: mac}. Windows — arp -a, Linux — /proc/net/arp."""
    table = {}
    try:
        if platform.system().lower() == 'windows':
            out = subprocess.run(['arp', '-a'], capture_output=True, text=True, timeout=3)
            for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})',
                                 out.stdout or ''):
                mac = m.group(2).lower()
                if mac != '00:00:00:00:00:00':
                    table[m.group(1)] = mac
        else:
            with open('/proc/net/arp') as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] != 'IP address':
                        mac = parts[3].lower()
                        if mac and mac != '00:00:00:00:00:00':
                            table[parts[0]] = mac
    except Exception:
        pass
    return table


# ------------------------------------------------- DNS / mDNS / PTR-имена

MDNS_PORT = 5353
MDNS_ADDR = '224.0.0.251'
DNS_PTR_TYPE = 12
DNS_CLASS_IN = 1
MAX_DNS_SERVERS = 4
_DNS_TIMEOUT = 0.8
_MDNS_UNICAST_TIMEOUT = 0.6
_MDNS_MULTICAST_TIMEOUT = 0.5


def _dns_encode_name(name):
    """Кодирование доменного имени в метки DNS."""
    out = bytearray()
    for label in str(name).rstrip('.').split('.'):
        raw = label.encode('utf-8', errors='ignore')[:63]
        out.append(len(raw))
        out += raw
    out.append(0)
    return bytes(out)


def _dns_decode_name(data, offset):
    """Декодирование имени из пакета (сжатие указателями). → (имя, следующий офсет)."""
    labels = []
    end = None
    for _ in range(128):
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            if end is None:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        if length & 0xC0:
            break
        offset += 1
        if offset + length > len(data):
            break
        labels.append(data[offset:offset + length].decode('utf-8', errors='replace'))
        offset += length
    return '.'.join(labels), (end if end is not None else len(data))


def _dns_build_query(qname, qid, qclass=DNS_CLASS_IN, flags=0x0100):
    """PTR-запрос: заголовок + QNAME + QTYPE(PTR) + QCLASS."""
    header = struct.pack('!HHHHHH', qid & 0xFFFF, flags, 1, 0, 0, 0)
    return header + _dns_encode_name(qname) + struct.pack('!HH', DNS_PTR_TYPE, qclass)


def _parse_ptr_answer(data, expect_id=None):
    """Ответ DNS/mDNS → PTR-имя. None — не наш пакет, ошибка или нет PTR."""
    if not data or len(data) < 12:
        return None
    qid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack('!HHHHHH', data[:12])
    if expect_id is not None and qid != (expect_id & 0xFFFF):
        return None
    if not flags & 0x8000 or flags & 0x000F:
        return None
    offset = 12
    for _ in range(min(qdcount, 32)):
        _qname, offset = _dns_decode_name(data, offset)
        offset += 4
    for _ in range(min(ancount, 64)):
        _aname, offset = _dns_decode_name(data, offset)
        if offset + 10 > len(data):
            return None
        rtype, rclass, _ttl, rdlen = struct.unpack('!HHIH', data[offset:offset + 10])
        offset += 10
        if offset + rdlen > len(data):
            return None
        if rtype == DNS_PTR_TYPE and (rclass & 0x7FFF) == DNS_CLASS_IN:
            ptr, _ = _dns_decode_name(data, offset)
            if ptr:
                return ptr
        offset += rdlen
    return None


def _reverse_name(ip):
    """'192.168.17.250' → '250.17.168.192.in-addr.arpa' (None — не IPv4)."""
    octets = str(ip).split('.')
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        return None
    return '.'.join(reversed(octets)) + '.in-addr.arpa'


def dns_reverse(ip, server, port=DNS_PORT, timeout=_DNS_TIMEOUT):
    """Прямой PTR-запрос к DNS-серверу по UDP. Имя или None."""
    qname = _reverse_name(ip)
    if not qname:
        return None
    qid = int(uuid.uuid4().hex[:4], 16)
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(_dns_build_query(qname, qid), (str(server), int(port)))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            sock.settimeout(remain)
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                return None
            name = _parse_ptr_answer(data, qid)
            if name:
                return name
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def system_reverse(ip):
    """PTR через системный резолвер (gethostbyaddr). Имя или None."""
    try:
        return socket.gethostbyaddr(str(ip))[0]
    except Exception:
        return None


def _mdns_lookup(qnames, target, timeout):
    """mDNS PTR-запрос (QU, QCLASS 0x8001) на target. Имя или None.

    Все варианты имён отправляются одним пакетом в общий таймаут: отдельные
    ответчики понимают либо '<rev>.local' (RFC 6762), либо голый '<rev>'.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        for qname in qnames:
            sock.sendto(_dns_build_query(qname, 0, qclass=0x8001, flags=0), target)
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            sock.settimeout(remain)
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                return None
            name = _parse_ptr_answer(data, 0)
            if name:
                return name
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def mdns_reverse(ip, timeout=_MDNS_UNICAST_TIMEOUT):
    """mDNS PTR: unicast-запрос самому хосту (порт 5353). Имя или None."""
    qname = _reverse_name(ip)
    if not qname:
        return None
    return _mdns_lookup((qname, qname + '.local'), (str(ip), MDNS_PORT), timeout)


def mdns_reverse_multicast(ip, timeout=_MDNS_MULTICAST_TIMEOUT):
    """mDNS PTR: multicast 224.0.0.251 (из Docker-контейнера обычно не работает)."""
    qname = _reverse_name(ip)
    if not qname:
        return None
    return _mdns_lookup((qname + '.local', qname), (MDNS_ADDR, MDNS_PORT), timeout)


def normalize_device_name(name, ip=None, allow_spaces=False):
    """Нормализация сетевого имени: trim, срез '.local', отбраковка мусора.

    None — пусто, совпадает с IP, длиннее 80 символов или (без allow_spaces)
    содержит пробелы. allow_spaces=True — для SSDP/HTML-title/SNMP-имён.
    FQDN (apidesk.dmydm.ru) сохраняется целиком — укорачиваем только '.local'.
    """
    if not name:
        return None
    name = str(name).strip().rstrip('.').strip()
    if not name or name == str(ip):
        return None
    if name.lower().endswith('.local'):
        name = name[:-len('.local')].rstrip('.').strip()
    if not name or name == str(ip) or len(name) > 80:
        return None
    if any(c.isspace() for c in name) and not allow_spaces:
        return None
    if any(c in name for c in '\r\n\t\x00'):
        return None
    return name


def resolv_conf_servers():
    """DNS-серверы из /etc/resolv.conf (без loopback)."""
    servers = []
    try:
        with open('/etc/resolv.conf', 'r') as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'nameserver':
                    server = parts[1].split('%')[0]
                    if server and not server.startswith('127.') and server != '::1':
                        if server not in servers:
                            servers.append(server)
    except Exception:
        pass
    return servers


def resolve_device_name(ip, dns_servers=(), multicast=True):
    """Имя устройства по IP: mDNS unicast → системный PTR → прямой DNS PTR → mDNS multicast.

    dns_servers — известные DNS (открытые :53 из скана, resolv.conf) — добавляются
    к списку. Возвращает нормализованное имя или None.
    """
    name = normalize_device_name(mdns_reverse(ip), ip)
    if name:
        return name
    name = normalize_device_name(system_reverse(ip), ip)
    if name:
        return name
    servers = []
    for server in list(dns_servers) + resolv_conf_servers():
        server = str(server).strip()
        if server and not server.startswith('127.') and server not in servers:
            servers.append(server)
    for server in servers[:MAX_DNS_SERVERS]:
        name = normalize_device_name(dns_reverse(ip, server), ip)
        if name:
            return name
    if multicast:
        name = normalize_device_name(mdns_reverse_multicast(ip), ip)
        if name:
            return name
    return None
