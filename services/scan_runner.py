# -*- coding: utf-8 -*-
"""Фоновое сканирование сети: джоба с прогрессом, отменой и классификацией устройств."""
import concurrent.futures
import ipaddress
import logging
import re
import socket
import threading
import time
import uuid

from services import scan_service as scanner

logger = logging.getLogger(__name__)

MAX_SCAN_HOSTS = 254
PROBE_TIMEOUT = 1.5

CAMERA_KEYWORDS = [
    'hikvision', 'dahua', 'dvr', 'nvr', 'ipcam', 'ip camera', 'webcam',
    'onvif', 'rtsp', 'reolink', 'amcrest', 'axis', 'xiaomi', 'smartvision',
    'ezviz', 'uniview', 'sunell', 'tp-camera', 'netvue', 'camera', 'wisenet',
    'samsung-camera', 'foscam', 'tapo', 'imou', 'yoosee', 'vivotek', 'mobotix',
    'network camera', 'ipc-', ' dome', 'video surveillance', 'h264', 'mjpeg'
]
CAMERA_SERVER_HEADERS = ['dvrds-webs', 'ipcamera', 'hikvision', 'dahua', 'webcam',
                         'gsoap', 'mjpg', 'netcam']

ROUTER_KEYWORDS = [
    'mikrotik', 'routeros', 'winbox', 'tp-link', 'tplink', 'd-link', 'dlink',
    'asus', 'keenetic', 'zyxel', 'unifi', 'ubiquiti', 'cisco', 'openwrt', 'luci',
    'pfsense', 'routers', 'netgear', 'huawei', 'juniper', 'fortinet', 'draytek',
    'fritz', 'totolink', 'mikrotik router', 'zywall', 'vodafone', 'linksys'
]

TYPE_LABELS = {'server': 'VNC', 'printer': 'Принтер', 'camera': 'Камера', 'router': 'Роутер'}

_lock = threading.Lock()
_job = None

_dns_lock = threading.Lock()
_known_dns = []


def _dns_candidates(open_cache):
    """DNS-серверы для PTR: открытые :53 из скана + resolv.conf (кэш ≤4)."""
    servers = []
    for (ip_addr, port), opened in open_cache.items():
        if port == scanner.DNS_PORT and opened and ip_addr not in servers:
            servers.append(ip_addr)
    for server in scanner.resolv_conf_servers():
        if server not in servers:
            servers.append(server)
    servers = [s for s in servers if not s.startswith('127.')][:scanner.MAX_DNS_SERVERS]
    with _dns_lock:
        for server in servers:
            if server not in _known_dns:
                _known_dns.append(server)
        del _known_dns[:-scanner.MAX_DNS_SERVERS]
        return list(_known_dns)


def _is_weak_name(name, ip):
    """Имя отсутствует или содержит IP (label+IP, VNC-IP) — стоит улучшить."""
    if not name:
        return True
    return bool(ip) and ip in str(name)


def _merge_name(old, ip, discovered):
    """Найденное имя: IP в старом имени заменяется, иначе берётся как есть."""
    if not old or old == ip:
        return discovered
    return str(old).replace(str(ip), discovered)


def _discover_name(ip, ports, get_web, dns_servers=()):
    """Полная цепочка поиска имени (метод за методом, пока имя не найдено):

    1. mDNS unicast → системный PTR → прямой DNS PTR → mDNS multicast;
    2. SSDP (friendlyName/modelName устройства);
    3. <title> страницы на открытых web-портах (только 200);
    4. SNMP sysName.
    """
    if not ip:
        return None
    ports = set(ports or ())
    name = scanner.resolve_device_name(ip, dns_servers)
    if name:
        return name
    for answer in scanner.ssdp_search(ip, 'ssdp:all', timeout=1.0) or []:
        info = scanner.ssdp_device_info(answer.get('location', ''))
        name = scanner.normalize_device_name(
            info.get('friendly_name') or info.get('model_name'), ip, allow_spaces=True)
        if name:
            return name
    for port in scanner.WEB_NAME_PORTS:
        if port not in ports:
            continue
        _headers, content, status = get_web(ip, port)
        if status != 200:
            continue
        name = scanner.normalize_device_name(scanner.html_title(content), ip,
                                              allow_spaces=True)
        if name:
            return name
    snmp = scanner.snmp_fields(scanner.snmp_get(ip, timeout=0.7))
    return scanner.normalize_device_name(snmp.get('sysName'), ip, allow_spaces=True)


def start(range_input):
    """Старт сканирования. Возвращает (job, already_running).

    Бросает ValueError с сообщением для пользователя при неверном вводе.
    """
    global _job
    range_input = (range_input or '').strip()
    if not range_input:
        raise ValueError('Укажите диапазон для сканирования')
    try:
        if '/' in range_input:
            ips = [str(ip) for ip in ipaddress.ip_network(range_input, strict=False).hosts()]
        else:
            ips = [str(ipaddress.ip_address(range_input))]
    except ValueError as e:
        raise ValueError(f'Неверный формат диапазона: {str(e)}')
    if len(ips) > MAX_SCAN_HOSTS:
        raise ValueError(f'Слишком большой диапазон (максимум {MAX_SCAN_HOSTS} адресов)')

    with _lock:
        if _job is not None and _job['status'] == 'running':
            return _job, True
        job = {
            'id': uuid.uuid4().hex[:12],
            'range': range_input,
            'total': len(ips),
            'checked': 0,
            'status': 'running',
            'error': None,
            'results': [],
            'unrecognized': [],
            'cancel': threading.Event(),
            'started': time.time(),
        }
        _job = job
    logger.info(f'Скан запущен: {range_input} ({len(ips)} IP), job={job["id"]}')
    threading.Thread(target=_run, args=(job, ips), daemon=True, name='scan-job').start()
    return job, False


def status():
    """Снимок состояния джобы (частичные результаты включены)."""
    with _lock:
        job = _job
        if job is None:
            return {'status': 'idle', 'total': 0, 'checked': 0, 'percent': 0,
                    'found_devices': 0, 'unrecognized_count': 0,
                    'results': [], 'unrecognized': [], 'error': None}
        return {
            'id': job['id'],
            'range': job['range'],
            'status': job['status'],
            'total': job['total'],
            'checked': job['checked'],
            'found_devices': len(job['results']),
            'unrecognized_count': len(job['unrecognized']),
            'percent': int(job['checked'] * 100 / job['total']) if job['total'] else 0,
            'error': job['error'],
            'results': list(job['results']),
            'unrecognized': list(job['unrecognized']),
            'elapsed': round(time.time() - job['started'], 1),
        }


def cancel():
    """Запрос отмены. True — флаг установлен, False — скан не выполняется."""
    with _lock:
        job = _job
    if job is None or job['status'] != 'running':
        return False
    job['cancel'].set()
    return True


def _run(job, ips):
    try:
        _scan_all(job, ips)
        with _lock:
            if job['status'] == 'running':
                job['status'] = 'cancelled' if job['cancel'].is_set() else 'done'
        logger.info(
            f'Скан {job["id"]} завершён: {job["status"]}, проверено {job["checked"]}/{job["total"]}, '
            f'найдено {len(job["results"])}, нераспознано {len(job["unrecognized"])}'
        )
    except Exception as e:
        logger.exception('Ошибка фонового сканирования')
        with _lock:
            job['status'] = 'error'
            job['error'] = str(e)


def _scan_all(job, ips):
    open_cache = {}
    web_cache = {}

    def check_port(ip, port):
        """Проверка открытого порта (с кэшем)."""
        key = (ip, int(port))
        if key in open_cache:
            return open_cache[key]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            result = sock.connect_ex((ip, int(port)))
            sock.close()
            open_cache[key] = result == 0
        except Exception:
            open_cache[key] = False
        return open_cache[key]

    def get_web(ip, port):
        """HTTP(S)-GET с кэшем: (headers, content, status)."""
        key = (ip, int(port))
        if key not in web_cache:
            web_cache[key] = scanner.http_probe(ip, port)
        return web_cache[key]

    def get_hostname(ip):
        """Имя хоста: полная цепочка поиска (DNS/mDNS/SSDP/заголовок/SNMP), фоллбэк — IP."""
        name = _discover_name(ip, _open_ports(ip), get_web, _dns_candidates(open_cache))
        return name or ip

    def _open_ports(ip):
        return sorted(p for (i, p), v in open_cache.items() if i == ip and v)

    def _camera_protocols(ip):
        ports = set(_open_ports(ip))
        protos = []
        if 554 in ports or 8554 in ports:
            protos.append('RTSP')
        if 80 in ports or 8080 in ports:
            protos.append('HTTP')
        if 443 in ports:
            protos.append('HTTPS')
        if 8000 in ports:
            protos.append('Hikvision SDK')
        if 37777 in ports:
            protos.append('Dahua SDK')
        return protos

    def _router_protocols(ip):
        ports = set(_open_ports(ip))
        protos = []
        if 80 in ports or 8080 in ports:
            protos.append('HTTP')
        if 443 in ports or 8443 in ports:
            protos.append('HTTPS')
        if 22 in ports:
            protos.append('SSH')
        if 23 in ports:
            protos.append('Telnet')
        if 161 in ports:
            protos.append('SNMP')
        if 1900 in ports:
            protos.append('UPnP')
        if 7547 in ports:
            protos.append('TR-069')
        return protos

    def _marker_name(content, markers):
        """Сниппет страницы вокруг маркера — как имя устройства."""
        if not content:
            return None
        lower = content.lower()
        for marker in markers:
            idx = lower.find(marker)
            if idx != -1:
                raw = content[max(0, idx - 40):idx + 60]
                snippet = ' '.join(re.sub(r'<[^>]+>', ' ', raw).split())
                if snippet:
                    return snippet[:80]
        return None

    def _web_scores(headers, content):
        """Оценка «камера vs роутер» по заголовкам и содержимому страницы."""
        cam_score = 0
        router_score = 0
        text = (content or '').lower()
        server = (headers or {}).get('server', '').lower()
        for kw in CAMERA_KEYWORDS:
            if kw in server or kw in text:
                cam_score += 1
        for h in CAMERA_SERVER_HEADERS:
            if h in server:
                cam_score += 2
        for kw in ROUTER_KEYWORDS:
            if kw in server or kw in text:
                router_score += 1
        return cam_score, router_score

    def get_printer_info(ip, port):
        """Подробная информация о принтере: веб-страница + SNMP."""
        info = {}
        _, content, status = get_web(ip, port)
        if status is not None and content:
            model_patterns = [
                r'<title[^>]*>([^<]+)</title>',
                r'Model[:\s]+([^\n<]+)',
                r'Printer[:\s]+([^\n<]+)',
                r'(HP|Canon|Epson|Brother|Xerox|Lexmark|Samsung|Kyocera|Ricoh|Panasonic|Sharp|'
                r'Toshiba|Konica|Minolta|OKI|Dell|Fuji|Zebra|Dymo)[\s-]+([A-Z0-9\-]+)'
            ]
            for pattern in model_patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    model = match.group(1).strip()
                    if len(model) > 3:
                        info['model'] = model
                        break

            lower = content.lower()
            for key in ('hp', 'canon', 'epson', 'brother', 'xerox', 'lexmark', 'samsung',
                        'kyocera', 'ricoh', 'panasonic', 'sharp', 'toshiba', 'konica',
                        'minolta', 'oki', 'dell', 'fuji', 'zebra', 'dymo'):
                if key in lower:
                    info['manufacturer'] = key.upper() if len(key) <= 5 else key.capitalize()
                    break

            for pattern in (r'Serial[:\s]+([A-Z0-9\-]+)', r'S/N[:\s]+([A-Z0-9\-]+)',
                            r'SerialNumber["\']:\s*["\']([^"\']+)', r'SN["\']:\s*["\']([^"\']+)'):
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    info['serial'] = match.group(1).strip()
                    break

            for pattern in (r'Toner[:\s]+([0-9]+)%', r'Black[:\s]+([0-9]+)%',
                            r'Ink[:\s]+([0-9]+)%', r'Cartridge[:\s]+([0-9]+)%'):
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    level = int(match.group(1))
                    if pattern.lower().startswith(('toner', 'black')):
                        info['toner_level'] = level
                    else:
                        info['ink_level'] = level
                    break

            for pattern in (r'Page\s+Count[:\s]+([0-9,]+)', r'Pages[:\s]+([0-9,]+)',
                            r'Counter[:\s]+([0-9,]+)'):
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    info['page_count'] = match.group(1).replace(',', '')
                    break

            for pattern in (r'Status[:\s]+([^\n<]+)',):
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    info['printer_status'] = match.group(1).strip()[:40]
                    break
            else:
                for word in ('Ready', 'Online', 'Offline', 'Error', 'Busy'):
                    if word.lower() in lower:
                        info['printer_status'] = word
                        break

        snmp = scanner.snmp_fields(scanner.snmp_get(ip, timeout=0.8))
        if snmp.get('sysName') and not info.get('model'):
            info['model'] = snmp['sysName'][:60]
        if snmp.get('sysDescr') and not info.get('model'):
            info['model'] = snmp['sysDescr'][:60]
        if snmp.get('sysLocation'):
            info['location'] = snmp['sysLocation'][:60]

        return {k: v for k, v in info.items() if v is not None}

    def camera_item(ip, port, name, web='', rtsp_url=''):
        return {
            'type': 'camera', 'ip': ip, 'port': port, 'name': name,
            'status': 'online', 'web_interface': web, 'rtsp_url': rtsp_url,
            'protocols': _camera_protocols(ip)
        }

    def router_item(ip, port, name, web=''):
        return {
            'type': 'router', 'ip': ip, 'port': port, 'name': name,
            'status': 'online', 'web_interface': web,
            'protocols': _router_protocols(ip)
        }

    def detect_camera(ip, hostname):
        """Поиск камеры: RTSP (+ONVIF для имени) → ONVIF → веб-API/скоринг/MJPEG → SDK."""
        for port in scanner.RTSP_PORTS:
            if not check_port(ip, port):
                continue
            data = scanner.rtsp_probe(ip, port)
            if not data:
                continue
            name = hostname
            for line in data.splitlines():
                if line.lower().startswith('server:'):
                    value = line.split(':', 1)[1].strip()
                    if value:
                        name = value[:80]
            m = re.search(r'WWW-Authenticate:\s*\S+\s+realm="?([^",\r\n]+)', data, re.IGNORECASE)
            if m and len(m.group(1).strip()) >= 3:
                name = m.group(1).strip()[:80]
            web = ''
            if name == ip:
                onvif = scanner.onvif_probe(ip, timeout=0.8) or {}
                candidate = onvif.get('name') or onvif.get('hardware')
                if candidate:
                    name = candidate[:80]
                if onvif.get('xaddr'):
                    xm = re.match(r'(https?://[^/]+)', onvif['xaddr'])
                    if xm:
                        web = xm.group(1)
            return camera_item(ip, port, name, web=web, rtsp_url=f'rtsp://{ip}:{port}/')

        onvif = scanner.onvif_probe(ip, timeout=0.8)
        if onvif:
            name = onvif.get('name') or onvif.get('hardware') or hostname
            web = ''
            if onvif.get('xaddr'):
                m = re.match(r'(https?://[^/]+)', onvif['xaddr'])
                if m:
                    web = m.group(1)
            item = camera_item(ip, 80, name, web=web or f'http://{ip}')
            item['onvif'] = True
            if onvif.get('hardware'):
                item['hardware'] = onvif['hardware']
            return item

        for port in scanner.CAMERA_WEB_PORTS:
            if not check_port(ip, port):
                continue
            headers, content, status = get_web(ip, port)
            if status is None:
                continue
            api = scanner.camera_api_probe(ip, port)
            cam_score, router_score = _web_scores(headers, content)
            is_cam = bool(api.get('model')) or (cam_score > 0 and cam_score >= router_score)
            if not is_cam and cam_score == 0 and router_score == 0:
                if scanner.mjpeg_probe(ip, port):
                    is_cam = True
            if not is_cam:
                continue
            title = scanner.html_title(content)
            name = (api.get('model')
                    or title
                    or _marker_name(content, CAMERA_KEYWORDS)
                    or hostname)
            web = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            item = camera_item(ip, port, name, web=web)
            item.update({k: v for k, v in api.items() if k != 'model'})
            if api.get('model') and api['model'] not in (item['name'],):
                item['model'] = api['model']
            return item

        for port, marker in ((8000, ('hikvision', 'ipc')), (37777, None)):
            if not check_port(ip, port):
                continue
            banner = scanner.tcp_banner(ip, port, timeout=1.5)
            if not banner:
                continue
            lower = banner.lower()
            if marker and not any(m in lower for m in marker):
                continue
            name = hostname
            if marker and 'hikvision' in lower:
                name = 'Hikvision IPC'
            item = camera_item(ip, port, name, rtsp_url=f'rtsp://{ip}:554/')
            item['sdk'] = True
            return item

        return None

    def detect_router(ip, hostname):
        """Поиск роутера: веб/SSH-маркер/Telnet/SNMP/SSDP/TR-069."""
        for port in scanner.ROUTER_WEB_PORTS:
            if not check_port(ip, port):
                continue
            headers, content, status = get_web(ip, port)
            if status is None:
                continue
            cam_score, router_score = _web_scores(headers, content)
            if router_score > 0 and router_score >= cam_score:
                name = (scanner.html_title(content)
                        or _marker_name(content, ROUTER_KEYWORDS)
                        or hostname)
                web = f'http{"s" if port == 443 else ""}://{ip}:{port}'
                return router_item(ip, port, name, web=web)

        if check_port(ip, 22):
            banner = scanner.tcp_banner(ip, 22)
            if scanner.ssh_banner_is_router(banner):
                name = hostname
                if any(k in banner.lower() for k in ('openwrt', 'dropbear', 'routeros')):
                    name = banner.split('\r\n')[0].strip()[:80]
                return router_item(ip, 22, name)

        if check_port(ip, 23):
            banner = scanner.tcp_banner(ip, 23)
            if banner and any(k in banner.lower() for k in
                              ('cisco', 'routeros', 'telnet', 'dlink', 'huawei', 'zyxel')):
                return router_item(ip, 23, banner.split('\r\n')[0].strip()[:80] or hostname)

        snmp = scanner.snmp_fields(scanner.snmp_get(ip, timeout=0.8))
        desc = snmp.get('sysDescr') or snmp.get('sysName') or ''
        if desc and any(k in desc.lower() for k in
                        ('cisco', 'mikrotik', 'routeros', 'huawei', 'zyxel', 'router',
                         'd-link', 'tp-link', 'dlink', 'switch', 'gateway')):
            return router_item(ip, 161, desc[:80],
                               web=f'http://{ip}' if 80 in _open_ports(ip) or 8080 in _open_ports(ip) else '')

        for answer in scanner.ssdp_search(ip, 'ssdp:all', timeout=1.0):
            haystack = ' '.join(str(answer.get(k, '')) for k in ('st', 'usn', 'server', 'location')).lower()
            if not any(k in haystack for k in ROUTER_KEYWORDS):
                continue
            info = scanner.ssdp_device_info(answer.get('location', ''))
            name = info.get('friendly_name') or info.get('model_name') or hostname
            location = answer.get('location', '')
            web = ''
            m = re.match(r'(https?://[^/]+)', location)
            if m:
                web = m.group(1)
            port = 443 if web.startswith('https') else 80
            return router_item(ip, port, name[:80], web=web)

        if check_port(ip, 7547):
            return router_item(ip, 7547, hostname)

        return None

    def check_ip(ip):
        """Проверка одного IP: TCP-проба ключевых портов, затем детект типов."""
        if job['cancel'].is_set():
            return None
        opened = scanner.tcp_probe_many(ip, scanner.KEY_PORTS, timeout=PROBE_TIMEOUT)
        for p in scanner.KEY_PORTS:
            open_cache[(ip, p)] = p in opened
        if not opened:
            return None

        notes = []
        rd_open = [p for p in scanner.RUSTDESK_PORTS if p in opened]
        if rd_open:
            notes.append('RustDesk: ' + ', '.join(str(p) for p in rd_open))

        found = []
        hostname = None

        def host():
            nonlocal hostname
            if hostname is None:
                hostname = get_hostname(ip)
            return hostname

        vnc_port = next((p for p in scanner.VNC_PORTS if p in opened), None)
        if vnc_port:
            banner = scanner.rfb_banner(ip, vnc_port)
            protocols = ['VNC']
            m = re.match(r'RFB\s+(\d+\.\d+)', banner or '')
            if m:
                protocols.append(f'RFB {m.group(1)}')
            found.append({
                'type': 'server', 'ip': ip, 'port': vnc_port,
                'name': f'VNC-{host()}', 'status': 'online',
                'protocols': protocols
            })
            logger.info(f'Найден VNC сервер: {ip}:{vnc_port}')

        for port in scanner.PRINTER_WEB_PORTS:
            if port not in opened:
                continue
            if port in (9100, 631, 515):
                is_printer = True
            else:
                headers, content, status = get_web(ip, port)
                is_printer = status is not None and scanner.looks_like_printer(headers, content)
            if not is_printer:
                continue
            hostname_value = host()
            info = get_printer_info(ip, port)
            title = scanner.html_title(get_web(ip, port)[1])
            display_name = info.get('model') or info.get('sysName') or title or hostname_value
            device_data = {
                'type': 'printer', 'ip': ip, 'port': port, 'name': display_name,
                'status': 'online',
                'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                'protocols': [{80: 'HTTP', 443: 'HTTPS', 9100: 'RAW', 631: 'IPP'}
                              .get(port, 'TCP')]
            }
            device_data.update(info)
            device_data['name'] = display_name
            found.append(device_data)
            logger.info(f'Найден принтер: {ip}:{port} - {display_name}')

        if not any(d['type'] == 'printer' for d in found):
            cam = detect_camera(ip, host())
            if cam:
                found.append(cam)
                logger.info(f'Найдена камера: {ip} - {cam.get("name")}')
            else:
                router = detect_router(ip, host())
                if router:
                    found.append(router)
                    logger.info(f'Найден роутер: {ip} - {router.get("name")}')

        web_name = None
        if not found:
            for port in sorted(opened):
                if port not in scanner.WEB_NAME_PORTS:
                    continue
                headers, content, _status = get_web(ip, port)
                web_name = scanner.fingerprint_server(headers, content)
                if web_name:
                    logger.info(f'Распознан сервис на {ip}:{port} - {web_name}')
                    break

        for device in found:
            label = TYPE_LABELS.get(device['type'], 'Устройство')
            if not device.get('name') or device['name'] == ip:
                device['name'] = f'{label} {ip}'
            if notes:
                device['notes'] = list(notes)

        if found:
            return {'devices': found}
        entry = {'ip': ip, 'ports': _open_ports(ip), 'notes': notes}
        if web_name:
            entry['name'] = web_name
        else:
            resolved = scanner.normalize_device_name(host(), ip)
            if resolved:
                entry['name'] = resolved
        return {'unrecognized': entry}

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)
    futures = {}
    for ip in ips:
        futures[executor.submit(check_ip, ip)] = ('tcp', ip)
        futures[executor.submit(scanner.icmp_ping, ip, 0.8)] = ('ping', ip)
    alive = set()
    pending = set(futures)
    try:
        while pending and not job['cancel'].is_set():
            done, pending = concurrent.futures.wait(
                pending, timeout=30,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                if job['cancel'].is_set():
                    break
                with _lock:
                    job['status'] = 'error'
                    job['error'] = 'Скан завис: 30 секунд без завершений'
                job['cancel'].set()
                logger.error(f'Скан {job["id"]}: завис (нет завершений 30 секунд)')
                break
            for future in done:
                if job['cancel'].is_set():
                    break
                kind, ip = futures[future]
                try:
                    outcome = future.result()
                except Exception as e:
                    logger.error(f'Ошибка при проверке IP {ip}: {e}')
                    if kind == 'tcp':
                        with _lock:
                            job['checked'] += 1
                    continue
                if kind == 'ping':
                    if outcome:
                        alive.add(ip)
                    continue
                with _lock:
                    job['checked'] += 1
                    if not outcome:
                        continue
                    if 'devices' in outcome:
                        job['results'].extend(outcome['devices'])
                    elif 'unrecognized' in outcome:
                        job['unrecognized'].append(outcome['unrecognized'])
    finally:
        executor.shutdown(wait=True, cancel_futures=job['cancel'].is_set())

    if alive and not job['cancel'].is_set() and job['status'] == 'running':
        with _lock:
            have = ({d.get('ip') for d in job['results']}
                    | {u.get('ip') for u in job['unrecognized']})
            for ip in sorted(alive - have):
                job['unrecognized'].append({
                    'ip': ip, 'ports': _open_ports(ip),
                    'notes': ['Отвечает на ping']
                })

    # Повторная проверка «живых, но не распознанных» после слива пула:
    # на пике нагрузки TCP-пробы гаснут (NAT/шторм SYN), порты теряются.
    retry = sorted(job['unrecognized'], key=lambda u: bool(u.get('ports')))
    if retry and not job['cancel'].is_set() and job['status'] == 'running':
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(check_ip, u['ip']): u for u in retry}
            pending = set(futs)
            while pending and not job['cancel'].is_set():
                done, pending = concurrent.futures.wait(
                    pending, timeout=30,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    logger.error('Повторная проверка зависла — пропускаем')
                    break
                for fut in done:
                    entry = futs[fut]
                    try:
                        outcome = fut.result()
                    except Exception as e:
                        logger.error(f'Ошибка повторной проверки {entry["ip"]}: {e}')
                        continue
                    if not outcome:
                        continue
                    with _lock:
                        if 'devices' in outcome:
                            if entry in job['unrecognized']:
                                job['unrecognized'].remove(entry)
                            job['results'].extend(outcome['devices'])
                        else:
                            new = outcome['unrecognized']
                            notes = list(new.get('notes') or [])
                            for n in entry.get('notes') or []:
                                if n not in notes:
                                    notes.append(n)
                            new['notes'] = notes
                            if entry in job['unrecognized']:
                                job['unrecognized'][
                                    job['unrecognized'].index(entry)] = new

    # Имена для записей без имени или с именем-IP (цепочка: DNS/mDNS → SSDP →
    # заголовок → SNMP) — параллельно, ≤30 сек. Сильные имена не трогаем.
    with _lock:
        targets = [(item, 'device') for item in job['results']
                   if _is_weak_name(item.get('name'), item.get('ip'))]
        targets += [(item, 'entry') for item in job['unrecognized']
                    if _is_weak_name(item.get('name'), item.get('ip'))]
    if targets and not job['cancel'].is_set() and job['status'] == 'running':
        servers = _dns_candidates(open_cache)
        name_ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix='scan-name')
        name_futs = {
            name_ex.submit(_discover_name, item.get('ip'),
                           _open_ports(item.get('ip')), get_web, servers): (item, kind)
            for item, kind in targets
        }
        pending = set(name_futs)
        try:
            while pending and not job['cancel'].is_set() and job['status'] == 'running':
                done, pending = concurrent.futures.wait(
                    pending, timeout=30,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    logger.error('Определение имён зависло — пропускаем')
                    break
                for fut in done:
                    item, kind = name_futs[fut]
                    try:
                        found_name = fut.result()
                    except Exception as e:
                        logger.error(f'Ошибка определения имени {item.get("ip")}: {e}')
                        continue
                    if not found_name:
                        continue
                    with _lock:
                        bucket = job['results'] if kind == 'device' else job['unrecognized']
                        for idx, cur in enumerate(bucket):
                            if cur is item:
                                bucket[idx] = dict(
                                    item,
                                    name=_merge_name(item.get('name'), item.get('ip'),
                                                     found_name))
                                break
        finally:
            name_ex.shutdown(wait=False,
                             cancel_futures=job['cancel'].is_set())

    arp = scanner.get_arp_table()
    if arp:
        with _lock:
            for item in job['results']:
                mac = arp.get(item.get('ip'))
                if mac:
                    item['mac'] = mac
            for item in job['unrecognized']:
                mac = arp.get(item.get('ip'))
                if mac:
                    item['mac'] = mac
