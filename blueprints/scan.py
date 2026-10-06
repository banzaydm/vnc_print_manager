# -*- coding: utf-8 -*-
"""Сканирование сети: VNC-порты, баннеры, принтеры, IP-камеры и роутеры."""
import concurrent.futures
import ipaddress
import logging
import platform
import socket
import subprocess

from flask import Blueprint, jsonify, request

bp = Blueprint('scan', __name__)
logger = logging.getLogger(__name__)
@bp.route('/api/scan', methods=['POST'])
def scan_network():
    """Сканирование сети для поиска VNC серверов и принтеров"""
    data = request.json or {}
    range_input = data.get('range', '').strip()
    
    if not range_input:
        return jsonify({'error': 'Укажите диапазон для сканирования'}), 400
    
    try:
        # Парсим диапазон (поддержка CIDR и простых IP)
        if '/' in range_input:
            network = ipaddress.ip_network(range_input, strict=False)
            ips = [str(ip) for ip in network.hosts()]
        else:
            # Одиночный IP
            ip = ipaddress.ip_address(range_input)
            ips = [str(ip)]
    except ValueError as e:
        return jsonify({'error': f'Неверный формат диапазона: {str(e)}'}), 400
    
    # Ограничиваем количество IP для безопасности
    if len(ips) > 254:
        return jsonify({'error': 'Слишком большой диапазон (максимум 254 адреса)'}), 400
    
    # Логируем для отладки
    logger.info(f'Сканирование диапазона {range_input}: {len(ips)} IP адресов')
    if ips:
        logger.info(f'Первый IP: {ips[0]}, Последний IP: {ips[-1]}')
    
    results = []
    
    def get_printer_info(ip, port):
        """Получить подробную информацию о принтере"""
        try:
            import urllib.request
            import urllib.error
            import re

            info = {
                'model': None,
                'serial': None,
                'status': None,
                'toner_level': None,
                'ink_level': None,
                'page_count': None,
                'manufacturer': None
            }
            
            # Пробуем получить информацию с веб-интерфейса
            url = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            
            # Попытка получить основную страницу
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=5) as response:
                    content = response.read(10000).decode('utf-8', errors='ignore')
                    
                    # Поиск модели в содержимом
                    model_patterns = [
                        r'<title[^>]*>([^<]+)</title>',
                        r'Model[:\s]+([^\n<]+)',
                        r'Printer[:\s]+([^\n<]+)',
                        r'(HP|Canon|Epson|Brother|Xerox|Lexmark|Samsung|Kyocera|Ricoh|Panasonic|Sharp|Toshiba|Konica|Minolta|OKI|Dell|Fuji|Zebra|Dymo)[\s-]+([A-Z0-9\-]+)'
                    ]
                    
                    for pattern in model_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            model = match.group(1).strip()
                            if len(model) > 3:  # Исключаем короткие совпадения
                                info['model'] = model
                                break
                    
                    # Определение производителя
                    manufacturer_patterns = {
                        'hp': 'HP', 'canon': 'Canon', 'epson': 'Epson',
                        'brother': 'Brother', 'xerox': 'Xerox', 'lexmark': 'Lexmark',
                        'samsung': 'Samsung', 'kyocera': 'Kyocera', 'ricoh': 'Ricoh',
                        'panasonic': 'Panasonic', 'sharp': 'Sharp', 'toshiba': 'Toshiba',
                        'konica': 'Konica', 'minolta': 'Minolta', 'oki': 'OKI',
                        'dell': 'Dell', 'fuji': 'Fuji', 'zebra': 'Zebra', 'dymo': 'Dymo'
                    }
                    
                    content_lower = content.lower()
                    for key, manufacturer in manufacturer_patterns.items():
                        if key in content_lower:
                            info['manufacturer'] = manufacturer
                            break
                    
                    # Поиск серийного номера
                    serial_patterns = [
                        r'Serial[:\s]+([A-Z0-9\-]+)',
                        r'S/N[:\s]+([A-Z0-9\-]+)',
                        r'SerialNumber["\']:\s*["\']([^"\']+)["\']',
                        r'SN["\']:\s*["\']([^"\']+)["\']'
                    ]
                    
                    for pattern in serial_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            info['serial'] = match.group(1).strip()
                            break
                    
                    # Поиск информации о тонере/чернилах
                    toner_patterns = [
                        r'Toner[:\s]+([0-9]+)%',
                        r'Black[:\s]+([0-9]+)%',
                        r'Ink[:\s]+([0-9]+)%',
                        r'Cartridge[:\s]+([0-9]+)%'
                    ]
                    
                    for pattern in toner_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            level = int(match.group(1))
                            if 'toner' in pattern.lower() or 'black' in pattern.lower():
                                info['toner_level'] = level
                            else:
                                info['ink_level'] = level
                            break
                    
                    # Поиск счетчика страниц
                    page_patterns = [
                        r'Page\s+Count[:\s]+([0-9,]+)',
                        r'Pages[:\s]+([0-9,]+)',
                        r'Counter[:\s]+([0-9,]+)'
                    ]
                    
                    for pattern in page_patterns:
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            info['page_count'] = match.group(1).replace(',', '')
                            break
                    
                    # Поиск статуса
                    status_patterns = [
                        r'Status[:\s]+([^\n<]+)',
                        r'Ready', r'Online', r'Offline', r'Error', r'Busy'
                    ]
                    
                    for pattern in status_patterns:
                        if isinstance(pattern, str):
                            if pattern.lower() in content_lower:
                                info['status'] = pattern
                                break
                        else:
                            match = re.search(pattern, content, re.IGNORECASE)
                            if match:
                                info['status'] = match.group(1).strip()
                                break
                    
            except Exception as e:
                logger.debug(f'Ошибка получения основной страницы принтера {ip}:{port}: {e}')
            
            # Пробуем SNMP для дополнительной информации
            try:
                import subprocess
                # Попытка получить системную информацию через SNMP
                result = subprocess.run([
                    'snmpget', '-v2c', '-c', 'public', ip,
                    '1.3.6.1.2.1.1.1.0',  # sysDescr
                    '1.3.6.1.2.1.1.5.0',  # sysName
                    '1.3.6.1.2.1.1.6.0'   # sysLocation
                ], capture_output=True, text=True, timeout=3)
                
                if result.returncode == 0:
                    snmp_data = result.stdout
                    
                    # Парсинг SNMP ответа
                    if 'sysDescr' in snmp_data and not info['model']:
                        desc_match = re.search(r'String:\s*(.+)', snmp_data)
                        if desc_match:
                            info['model'] = desc_match.group(1).strip()
                    
                    if 'sysName' in snmp_data and not info['model']:
                        name_match = re.search(r'String:\s*(.+)', snmp_data.split('sysName')[1])
                        if name_match:
                            info['model'] = name_match.group(1).strip()
                            
            except Exception as e:
                logger.debug(f'SNMP недоступен для {ip}: {e}')
            
            # Удаляем None значения
            info = {k: v for k, v in info.items() if v is not None}
            
            return info
            
        except Exception as e:
            logger.debug(f'Ошибка получения информации о принтере {ip}: {e}')
            return {}
    
    def get_hostname(ip):
        """Получить имя хоста по IP через reverse DNS"""
        try:
            import socket
            hostname = socket.gethostbyaddr(ip)[0]
            return hostname
        except Exception:
            return ip
    
    def check_ip(ip):
        """Проверка одного IP адреса"""
        try:
            logger.debug(f'Начало проверки IP: {ip}')
            
            # Сначала проверяем ping
            if not ping_host(ip):
                logger.debug(f'IP {ip} не отвечает на ping')
                return None
            
            logger.debug(f'IP {ip} отвечает на ping, проверяем порты')
            found_devices = []
            
            # Проверяем VNC (порт 5900)
            if check_port(ip, 5900):
                hostname = get_hostname(ip)
                found_devices.append({
                    'type': 'server',
                    'ip': ip,
                    'port': 5900,
                    'name': f'VNC-{hostname}',
                    'status': 'online',
                    'protocols': ['VNC']
                })
                logger.info(f'Найден VNC сервер: {ip}:5900')
            
            # Проверяем веб-интерфейсы принтеров (порты 80, 443, 9100, 631)
            for port in [80, 443, 9100, 631]:
                if check_port(ip, port):
                    logger.debug(f'IP {ip} имеет открытый порт {port}, проверяем на принтер')
                    # Дополнительная проверка на принтер
                    if is_likely_printer(ip, port):
                        hostname = get_hostname(ip)
                        printer_info = get_printer_info(ip, port)
                        
                        # Используем модель из printer_info, если доступна
                        display_name = printer_info.get('model', hostname)
                        
                        device_data = {
                            'type': 'printer',
                            'ip': ip,
                            'port': port,
                            'name': display_name,
                            'status': 'online',
                            'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                            'protocols': [{
                                80: 'HTTP', 443: 'HTTPS', 9100: 'RAW', 631: 'IPP', 515: 'LPR'
                            }.get(port, 'TCP')]
                        }
                        
                        # Добавляем подробную информацию, если доступна
                        device_data.update(printer_info)
                        
                        found_devices.append(device_data)
                        logger.info(f'Найден принтер: {ip}:{port} - {display_name}')
            
            # Ищем камеры и роутеры (если на IP не найден принтер)
            if not any(d['type'] == 'printer' for d in found_devices):
                hostname = get_hostname(ip)
                cam = detect_camera(ip, hostname)
                if cam:
                    found_devices.append(cam)
                    logger.info(f'Найдена камера: {ip} - {cam.get("name")}')
                else:
                    router = detect_router(ip, hostname)
                    if router:
                        found_devices.append(router)
                        logger.info(f'Найден роутер: {ip} - {router.get("name")}')

            if found_devices:
                logger.debug(f'IP {ip}: найдено {len(found_devices)} устройств')
            else:
                logger.debug(f'IP {ip}: устройств не найдено')
            
            return found_devices if found_devices else None
            
        except Exception as e:
            logger.error(f'Ошибка при проверке IP {ip}: {e}')
            return None
    
    def ping_host(ip):
        """Проверка доступности хоста"""
        try:
            if platform.system().lower() == 'windows':
                cmd = ['ping', '-n', '1', '-w', '1000', ip]
            else:
                cmd = ['ping', '-c', '1', '-W', '1', ip]
            
            logger.debug(f'Проверка ping для {ip}: {" ".join(cmd)}')
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            logger.debug(f'Ping {ip}: returncode={result.returncode}, stdout={result.stdout[:100]}')
            return result.returncode == 0
        except FileNotFoundError:
            # Если ping недоступен (например в Docker без ping), пропускаем проверку
            logger.warning(f'Ping недоступен, пропускаем проверку для {ip}')
            return True  # Считаем что хост доступен, переходим к проверке портов
        except Exception as e:
            logger.error(f'Ошибка ping для {ip}: {e}')
            return False
    
    open_cache = {}

    def check_port(ip, port):
        """Проверка открытого порта (с кэшем)"""
        key = (ip, int(port))
        if key in open_cache:
            return open_cache[key]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            sock.close()
            open_cache[key] = result == 0
            logger.debug(f'Проверка порта {ip}:{port} - {"открыт" if result == 0 else "закрыт"}')
            return open_cache[key]
        except Exception as e:
            logger.debug(f'Ошибка проверки порта {ip}:{port}: {e}')
            open_cache[key] = False
            return False

    def _cached_open_ports(ip):
        """Открытые порты IP из уже выполненного сканирования (без новых запросов)."""
        return {p for (i, p), v in open_cache.items() if i == ip and v}

    def _camera_protocols(ip):
        ports = _cached_open_ports(ip)
        protos = []
        if 554 in ports:
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
        ports = _cached_open_ports(ip)
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

    def is_likely_printer(ip, port):
        """Улучшенная проверка на принтер"""
        if port in (9100, 631, 515):
            return True
        try:
            import urllib.request

            url = f'http{"s" if port == 443 else ""}://{ip}:{port}'
            req = urllib.request.Request(url, method='GET')

            with urllib.request.urlopen(req, timeout=5) as response:
                headers = dict(response.headers)
                content = response.read(5000).decode('utf-8', errors='ignore').lower()

                printer_keywords = [
                    'printer', 'hp', 'canon', 'epson', 'brother', 'xerox',
                    'lexmark', 'samsung', 'kyocera', 'ricoh', 'panasonic',
                    'sharp', 'toshiba', 'konica', 'minolta', 'oki',
                    'dell', 'fuji', 'zebra', 'dymo'
                ]

                server = headers.get('Server', '').lower()
                content_type = headers.get('Content-Type', '').lower()
                header_match = any(keyword in server for keyword in printer_keywords)
                content_type_match = any(keyword in content_type for keyword in printer_keywords)
                title_match = '<title>' in content and any(keyword in content for keyword in printer_keywords)
                path_match = any(path in content for path in ['/printer', '/status', '/main', '/device', '/web'])
                is_printer = header_match or content_type_match or title_match or path_match

                if is_printer:
                    logger.info(
                        f'Найден принтер {ip}:{port} - признаки: header={header_match}, '
                        f'content_type={content_type_match}, title={title_match}, path={path_match}'
                    )
                return is_printer

        except Exception as e:
            logger.debug(f'Ошибка проверки принтера {ip}:{port}: {e}')
            return False

    web_probe_cache = {}

    def get_web(ip, port):
        """HTTP(S) GET: возвращает (headers_lower, content_lower, status) с кэшем."""
        key = (ip, int(port))
        if key in web_probe_cache:
            return web_probe_cache[key]
        result = (None, None, None)
        try:
            import urllib.request
            import urllib.error
            scheme = 'https' if port == 443 else 'http'
            req = urllib.request.Request(f'{scheme}://{ip}:{port}/', method='GET', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as response:
                raw = response.read(20000)
                try:
                    content = raw.decode('utf-8', errors='ignore').lower()
                except Exception:
                    content = ''
                headers = {k.lower(): (v or '').lower() for k, v in response.headers.items()}
                result = (headers, content, response.status)
        except urllib.error.HTTPError as e:
            result = ({'status': 'HTTP', 'server': ''}, str(e.code), e.code)
        except Exception as e:
            logger.debug(f'get_web {ip}:{port} failed: {e}')
        web_probe_cache[key] = result
        return result

    def banner_grab(ip, port, read=True):
        """Чтение TCP-баннера с порта."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            sock.connect((ip, port))
            if read:
                data = sock.recv(1024)
                sock.close()
                return data.decode('utf-8', errors='ignore')
            sock.close()
            return ''
        except Exception:
            return ''

    def rtsp_probe(ip, port=554):
        """Проверка RTSP-сервера: отправка OPTIONS, возврат ответа."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.5)
            sock.connect((ip, port))
            sock.send(f'OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode())
            data = sock.recv(4096).decode('utf-8', errors='ignore')
            sock.close()
            return data if 'RTSP/1.0' in data else ''
        except Exception:
            return ''

    CAMERA_KEYWORDS = [
        'hikvision', 'dahua', 'dvr', 'nvr', 'ipcam', 'ip camera', 'webcam',
        'onvif', 'rtsp', 'reolink', 'amcrest', 'axis', 'xiaomi', 'smartvision',
        'ezviz', 'uniview', 'sunell', 'tp-camera', 'netvue', 'camera', 'wisenet', 'samsung-camera'
    ]
    CAMERA_SERVER_HEADERS = ['dvrds-webs', 'ipcamera', 'hikvision', 'dahua', 'webcam']

    ROUTER_KEYWORDS = [
        'mikrotik', 'routeros', 'winbox', 'tp-link', 'tplink', 'd-link', 'dlink',
        'asus', 'keenetic', 'zyxel', 'unifi', 'ubiquiti', 'cisco', 'openwrt', 'luci',
        'pfsense', 'routers', 'netgear', 'huawei', 'juniper', 'fortinet', 'draytek',
        'fritz', 'totolink', 'mikrotik router', 'zywall', 'vodafone', 'linksys'
    ]

    def _web_scores(headers, content):
        cam_score = 0
        router_score = 0
        text = (content or '')
        server = (headers or {}).get('server', '')
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

    def _camera_from_web(ip, hostname, port, web):
        headers, content, _ = web
        cam_score, router_score = _web_scores(headers, content)
        if cam_score > router_score and cam_score > 0:
            name = hostname
            for marker in ('hikvision', 'dahua', 'reolink', 'amcrest', 'axis', 'uniview', 'wisenet', 'ezviz'):
                idx = content.find(marker)
                if idx != -1:
                    snippet = content[max(0, idx - 40):idx + 60].split('<')[0].strip()
                    if snippet:
                        name = snippet if len(snippet) < 60 else snippet[:60]
                        break
            return {
                'type': 'camera',
                'ip': ip,
                'port': port,
                'name': name,
                'status': 'online',
                'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                'protocols': _camera_protocols(ip)
            }
        return None

    def detect_camera(ip, hostname):
        """Поиск камеры видеонаблюдения по RTSP/HTTP/ONVIF/SDK/MJPEG."""
        result = None

        rtsp_data = rtsp_probe(ip) if check_port(ip, 554) else ''
        if rtsp_data:
            name = hostname
            server_line = [ln for ln in rtsp_data.splitlines() if ln.lower().startswith('server:')]
            if server_line:
                model = server_line[0].split(':', 1)[1].strip()
                if model:
                    name = model if len(model) < 60 else model[:60]
            result = {
                'type': 'camera',
                'ip': ip,
                'port': 554,
                'name': name,
                'status': 'online',
                'rtsp_url': f'rtsp://{ip}:554/',
                'web_interface': '',
                'protocols': _camera_protocols(ip)
            }
            return result

        for port in (80, 443, 8080):
            if check_port(ip, port):
                web = get_web(ip, port)
                cam = _camera_from_web(ip, hostname, port, web)
                if cam:
                    return cam

        if check_port(ip, 8000):  # Hikvision SDK / IPC
            b = banner_grab(ip, 8000)
            if 'hikvision' in b.lower() or 'ipc' in b.lower():
                return {'type': 'camera', 'ip': ip, 'port': 8000, 'name': 'Hikvision IPC', 'status': 'online', 'rtsp_url': f'rtsp://{ip}:554/', 'web_interface': '', 'protocols': _camera_protocols(ip)}
            if b:
                return {'type': 'camera', 'ip': ip, 'port': 8000, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _camera_protocols(ip)}

        if check_port(ip, 37777):  # Dahua SDK
            b = banner_grab(ip, 37777)
            if b:
                return {'type': 'camera', 'ip': ip, 'port': 37777, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _camera_protocols(ip)}

        return result

    def _router_from_web(ip, hostname, port, web):
        headers, content, _ = web
        cam_score, router_score = _web_scores(headers, content)
        if router_score > 0 and router_score >= cam_score:
            name = hostname
            for marker in ('mikrotik', 'routeros', 'tp-link', 'd-link', 'keenetic', 'unifi', 'openwrt', 'asus', 'cisco'):
                idx = content.find(marker)
                if idx != -1:
                    snippet = content[max(0, idx - 30):idx + 50].split('<')[0].strip()
                    if snippet:
                        name = snippet if len(snippet) < 60 else snippet[:60]
                        break
            return {
                'type': 'router',
                'ip': ip,
                'port': port,
                'name': name,
                'status': 'online',
                'web_interface': f'http{"s" if port == 443 else ""}://{ip}:{port}',
                'protocols': _router_protocols(ip)
            }
        return None

    def detect_router(ip, hostname):
        """Поиск роутера/сетевого оборудования по веб/SSH/Telnet/SNMP/UPnP/TR-069."""
        for port in (80, 443, 8080, 8443):
            if check_port(ip, port):
                web = get_web(ip, port)
                router = _router_from_web(ip, hostname, port, web)
                if router:
                    return router

        if check_port(ip, 22):
            banner = banner_grab(ip, 22)
            if banner and 'SSH-2.0' in banner:
                name = hostname
                if 'openwrt' in banner.lower() or 'dropbear' in banner.lower() or 'routeros' in banner.lower():
                    name = banner.split('\r\n')[0].strip()
                return {'type': 'router', 'ip': ip, 'port': 22, 'name': name, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        if check_port(ip, 23):
            banner = banner_grab(ip, 23)
            if banner and any(k in banner.lower() for k in ('cisco', 'routeros', 'telnet', 'dlink', 'huawei', 'zyxel')):
                return {'type': 'router', 'ip': ip, 'port': 23, 'name': banner.split('\r\n')[0].strip() or hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        if check_port(ip, 7547):  # TR-069
            return {'type': 'router', 'ip': ip, 'port': 7547, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}

        try:
            import subprocess
            r = subprocess.run(
                ['snmpget', '-v2c', '-c', 'public', '-t', '1', '-r', '0', ip, '1.3.6.1.2.1.1.1.0'],
                capture_output=True, text=True, timeout=3
            )
            if r.returncode == 0 and ('STRING' in r.stdout or 'OID' in r.stdout):
                desc = r.stdout.strip()
                if any(k in desc.lower() for k in ('cisco', 'mikrotik', 'routeros', 'huawei', 'zyxel', 'router', 'd-link', 'tp-link', 'dlink', 'switch')):
                    return {'type': 'router', 'ip': ip, 'port': 161, 'name': desc[:60], 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            import socket as _s
            msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                   'MAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n').encode()
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.settimeout(2)
            s.sendto(msg, (ip, 1900))
            data, _ = s.recvfrom(2048)
            s.close()
            resp = data.decode('utf-8', errors='ignore').lower()
            if 'internetgatewaydevice' in resp or 'location:' in resp:
                return {'type': 'router', 'ip': ip, 'port': 1900, 'name': hostname, 'status': 'online', 'web_interface': '', 'protocols': _router_protocols(ip)}
        except Exception:
            pass

        return None

    # Параллельная проверка IP адресов
    checked_count = 0
    responsive_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        future_to_ip = {executor.submit(check_ip, ip): ip for ip in ips}
        
        for future in concurrent.futures.as_completed(future_to_ip):
            try:
                checked_count += 1
                devices = future.result()
                if devices:
                    responsive_count += 1
                    results.extend(devices)
            except Exception as e:
                logger.error(f'Ошибка при проверке IP: {e}')
    
    logger.info(f'Сканирование завершено: проверено {checked_count}/{len(ips)} IP, ответило {responsive_count}, найдено устройств: {len(results)}')
    
    return jsonify({
        'success': True,
        'range': range_input,
        'scanned_ips': len(ips),
        'found_devices': len(results),
        'results': results
    })


