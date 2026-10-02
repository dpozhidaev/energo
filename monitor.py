#!/usr/bin/env python3
"""Ежедневная сводка отключений в Песках. Python 3.11+."""
import argparse
import concurrent.futures
import json
import smtplib
import ssl
from email.message import EmailMessage
from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit, urlencode
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = 'https://rosseti-lenenergo.ru/planned_work/'
CA = Path(__file__).parent / 'certs/russian_trusted_root_ca.pem'
MSK = ZoneInfo('Europe/Moscow')
DEFAULT_CONFIG = Path(__file__).parent / 'config.json'
DEFAULT_SENT_STATE = Path(__file__).parent / 'sent.json'
# Облачные раннеры GitHub Actions размещены за пределами РФ; сайт закрыт DDoS-Guard geoblock'ом
# ("the website owner has restricted access from your current IP address") для таких адресов.
# Список ниже обновляется автоматически (раз в ~30 минут) и включает геолокацию и протокол каждого узла.
MONOSANS_PROXY_LIST_URL = 'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json'
PROXYSCRAPE_URL = 'https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=RU&anonymity=all'
GEONODE_URL = 'https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&country=RU&protocols=http%2Chttps'


@dataclass(frozen=True)
class Config:
    check_time: str = '09:00'
    settlement: str = 'Пески'
    district: str = 'Выборгский'
    region_id: str = '344'
    res_id: str = '370'
    channel: str = 'telegram'
    telegram_chat_id: str = ''
    email_to: str = ''

    def __post_init__(self):
        if not all(isinstance(v, str) for v in self.__dict__.values()):
            raise ValueError('Все параметры config.json должны быть строками')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.check_time):
            raise ValueError('check_time должен иметь формат HH:MM, время Москвы')
        if self.channel not in ('telegram', 'email'):
            raise ValueError('channel должен быть telegram или email')
        if not self.settlement.strip() or not self.district.strip():
            raise ValueError('settlement и district не могут быть пустыми')

    @property
    def params(self):
        return dict(reg=self.region_id, city='', date_start='', date_finish='', res=self.res_id, street='')

    @property
    def source(self):
        return BASE + '?' + urlencode({'reg': self.region_id, 'res': self.res_id})

    @property
    def label(self):
        return f'{self.settlement}, {self.district} район'


def load_config(path=DEFAULT_CONFIG):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return Config(**data)


def write_schedules(config, root=None):
    root = Path(root) if root else Path(__file__).parent
    hour, minute = map(int, config.check_time.split(':'))
    workflow = root / '.github/workflows/check.yml'
    text = workflow.read_text()
    text, count = re.subn(r"cron: '[^']+'[^\n]*", f"cron: '{minute} {(hour - 3) % 24} * * *' # {config.check_time} Europe/Moscow", text)
    if count != 1:
        raise ValueError('Ожидалось одно расписание в workflow')
    timer = root / 'deploy/peski-monitor.timer'
    timer_text, count = re.subn(r'OnCalendar=[^\n]+', f'OnCalendar=*-*-* {config.check_time}:00 Europe/Moscow', timer.read_text())
    if count != 1:
        raise ValueError('Не найдено расписание systemd')
    workflow.write_text(text)
    timer.write_text(timer_text)



@dataclass(frozen=True)
class Outage:
    record_id: str
    address: str
    start: datetime
    end: datetime
    comment: str


def clean(value):
    return ' '.join(value.split())


def parse_page(html, config=Config()):
    settlement = re.compile(r"^(?:(?:п\.?|пос\.?|поселок|посёлок|д\.?|деревня|с\.?|село|г\.?|город)\s+)?" + re.escape(config.settlement) + r"(?=$|[\s,.;])", re.I)
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.tableous_facts')
    if table is None:
        raise ValueError('Таблица отключений не найдена: возможно, сайт изменился или недоступен')
    headers = clean(table.get_text(' ', strip=True)).lower()
    for required in ('адрес', 'плановое время начала', 'плановое время восстановления'):
        if required not in headers:
            raise ValueError('Изменилась структура таблицы отключений')
    outages, ids = [], []
    for row in table.select('tbody tr'):
        cells = row.find_all('td', recursive=False)
        if not cells:
            continue
        if len(cells) != 11 or not row.get('data-record-id'):
            raise ValueError('Неожиданная строка в таблице; результат не считается пустым')
        vals = [clean(c.get_text(' ', strip=True)) for c in cells]
        ids.append(row['data-record-id'])
        # Проверяем район и населённый пункт, а не произвольное упоминание в комментарии.
        addresses = [clean(s) for s in cells[2].get_text('|', strip=True).split('|')]
        if not re.search(r'(?<!\w)' + re.escape(config.district) + r'(?!\w)', vals[1], re.I):
            continue
        matches = [a for a in addresses if settlement.search(a)]
        if not matches:
            continue
        start = datetime.strptime(vals[3] + ' ' + vals[4], '%d-%m-%Y %H:%M').replace(tzinfo=MSK)
        end = datetime.strptime(vals[5] + ' ' + vals[6], '%d-%m-%Y %H:%M').replace(tzinfo=MSK)
        if end < start:
            raise ValueError('Время окончания отключения раньше начала')
        # Только строки адреса, где встречается сам посёлок — остальные посёлки общей записи не нужны.
        outages.append(Outage(row['data-record-id'], '; '.join(matches), start, end, vals[9]))
    pages = {1}
    for link in soup.select('a[href]'):
        label = link.get_text(strip=True)
        # У сайта есть ошибочная стрелка «следующая» даже на последней странице.
        if not (label.isdigit() or label == '»'):
            continue
        for value in parse_qs(urlsplit(link['href']).query).get('PAGEN_1', []):
            if value.isdigit() and int(value) > 0:
                pages.add(int(value))
    if max(pages) > 100:
        raise ValueError('Слишком много страниц; требуется проверка сайта')
    return outages, set(range(1, max(pages) + 1)), tuple(ids)


def site_session():
    session = requests.Session()
    session.headers['User-Agent'] = 'PeskiOutageMonitor/1.0 (daily personal notification)'
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


SUPPORTED_PROXY_SCHEMES = ('http', 'socks4', 'socks5')


def fetch_candidate_proxies(session):
    """Собирает (схема, адрес) российских прокси из открытых бесплатных списков.

    Большинство рабочих узлов на практике оказываются socks4/socks5, а не обычным HTTP-прокси,
    поэтому monosans/proxy-list (с гео-тегами и протоколом на узел) идёт первым источником.
    """
    candidates = []
    try:
        response = session.get(MONOSANS_PROXY_LIST_URL, timeout=(5, 20))
        response.raise_for_status()
        ru_nodes = []
        for item in response.json():
            geo = (item.get('geolocation') or {}).get('country') or {}
            scheme, host, port = item.get('protocol'), item.get('host'), item.get('port')
            if geo.get('iso_code') == 'RU' and scheme in SUPPORTED_PROXY_SCHEMES and host and port:
                ru_nodes.append((item.get('timeout', 999), scheme, f'{host}:{port}'))
        ru_nodes.sort()
        candidates += [(scheme, address) for _, scheme, address in ru_nodes]
    except (requests.RequestException, ValueError):
        pass
    for url in (PROXYSCRAPE_URL, GEONODE_URL):
        try:
            response = session.get(url, timeout=(5, 15))
            response.raise_for_status()
        except requests.RequestException:
            continue
        if url is GEONODE_URL:
            try:
                for item in response.json().get('data', []):
                    host, port = item.get('ip'), item.get('port')
                    if host and port:
                        candidates.append(('http', f'{host}:{port}'))
            except ValueError:
                pass
        else:
            candidates += [('http', line.strip()) for line in response.text.splitlines()
                           if re.fullmatch(r'[\d.]+:\d+', line.strip())]
    seen, unique = set(), []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def probe_proxy(scheme, address, config=Config()):
    """Проверяет, отдаёт ли прокси настоящую страницу отключений (а не блокировку/заглушку)."""
    proxies = {'http': f'{scheme}://{address}', 'https': f'{scheme}://{address}'}
    try:
        response = requests.get(BASE, params=config.params, proxies=proxies, timeout=(6, 15), verify=str(CA))
        response.raise_for_status()
    except requests.RequestException:
        return None
    return proxies if 'tableous_facts' in response.text else None


def find_working_proxy(session, config=Config(), limit=80, max_workers=20):
    """Подбирает рабочий российский прокси из открытых списков, проверяя кандидатов параллельно."""
    candidates = fetch_candidate_proxies(session)[:limit]
    if not candidates:
        raise RuntimeError('Не удалось получить список прокси для РФ')
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {pool.submit(probe_proxy, scheme, address, config): (scheme, address) for scheme, address in candidates}
        for future in concurrent.futures.as_completed(futures):
            proxies = future.result()
            if proxies:
                return proxies
    finally:
        # Не ждём медленные/зависшие проверки остальных кандидатов после первого успеха.
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(f'Не найден работающий прокси в РФ среди {len(candidates)} адресов')


def collect_with_fallback(session, now, config=Config()):
    """Сначала пробует прямой доступ; при недоступности сайта подбирает рабочий прокси в РФ и повторяет."""
    try:
        return collect(session, now, config)
    except (requests.RequestException, ValueError):
        proxies = find_working_proxy(session, config)
        session.proxies.update(proxies)
        print(f'Прямой доступ к сайту недоступен; используется прокси {proxies["https"]}')
        return collect(session, now, config)


def collect(session, now, config=Config()):
    pending, visited, signatures, result = {1}, set(), set(), {}
    while pending:
        page = min(pending)
        pending.remove(page)
        # Ссылки сайта теряют фильтры: переносим только номер страницы.
        response = session.get(BASE, params={**config.params, 'PAGEN_1': page}, timeout=(10, 45), verify=str(CA))
        response.raise_for_status()
        response.encoding = 'utf-8'
        rows, pages, signature = parse_page(response.text, config)
        if signature and signature in signatures:
            raise ValueError('Сайт повторяет страницу вместо перехода к следующей')
        signatures.add(signature)
        visited.add(page)
        pending.update(pages - visited)
        for row in rows:
            if row.end > now:
                result[row.record_id] = row
    print(f'Проверено страниц: {len(visited)}; актуальных записей ({config.label}): {len(result)}')
    return sorted(result.values(), key=lambda r: (r.start, r.record_id))


SENT_STATE_LIMIT = 10


def load_sent_state(path=DEFAULT_SENT_STATE):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []


def save_sent_state(state, path=DEFAULT_SENT_STATE):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def filter_unsent(rows, sent):
    return [row for row in rows if row.record_id not in sent]


def update_sent_state(sent, new_rows, limit=SENT_STATE_LIMIT):
    """Хранит только последние `limit` отправленных record_id, чтобы sent.json не рос бесконечно."""
    return (sent + [row.record_id for row in new_rows])[-limit:]


def address_line(row, config=Config()):
    """Строка с улицей, если она есть у посёлка в записи; иначе общий адрес посёлка и района.
    Остальные посёлки общей записи сайта (row.address уже отфильтрован в parse_page) не выводятся."""
    bare = re.compile(r"^(?:п\.?|пос\.?|поселок|посёлок|д\.?|деревня|с\.?|село|г\.?|город)?\s*"
                      + re.escape(config.settlement) + r"\s*$", re.I)
    parts = [a for a in row.address.split('; ') if a]
    if any(not bare.fullmatch(a) for a in parts):
        return '; '.join(parts) + f', {config.district} район'
    return f'пос.{config.label}'


def messages(rows, now, config=Config()):
    # Одно сообщение на запись: интересует только сам факт и время отключения в конкретном
    # посёлке, а не полный (часто на 10+ посёлков) список адресов из ячейки сайта.
    texts = []
    for row in rows:
        # Без parse_mode: текст сайта (комментарий) не становится HTML/Markdown.
        text = (f'Плановое отключение электричества!\n'
                f'{row.start:%d.%m.%Y %H:%M} — {row.end:%d.%m.%Y %H:%M} МСК\n'
                f'Адрес: {address_line(row, config)}\n'
                f'Комментарий: {row.comment or "не указан"}\n'
                f'Запись: {row.record_id}\n'
                f'Проверка: {now:%d.%m.%Y %H:%M} МСК\n'
                f'\nИсточник: {config.source}')
        texts.append(text[:4096])  # лимит Telegram на длину sendMessage
    return texts


def telegram(method, payload):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Не задан TELEGRAM_BOT_TOKEN')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/{method}', json=payload, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            raise ValueError('Telegram отклонил запрос')
        return data['result']
    except (requests.RequestException, ValueError):
        # Исключение requests содержит URL с токеном — не выводим его в журналы.
        raise RuntimeError('Запрос к Telegram не выполнен; проверьте токен, chat_id и доступ к сети') from None


def validate_destination(config):
    if config.channel == 'telegram':
        if not os.environ.get('TELEGRAM_BOT_TOKEN') or not (os.environ.get('TELEGRAM_CHAT_ID') or config.telegram_chat_id):
            raise ValueError('Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (или telegram_chat_id в config.json)')
    elif not all(os.environ.get(k) for k in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD')) or not config.email_to:
        raise ValueError('Для email нужны SMTP_HOST, SMTP_USER, SMTP_PASSWORD и email_to')


def send(text, config=Config()):
    validate_destination(config)
    if config.channel == 'telegram':
        chat = os.environ.get('TELEGRAM_CHAT_ID') or config.telegram_chat_id
        telegram('sendMessage', dict(chat_id=chat, text=text, link_preview_options={'is_disabled': True}))
        return
    message = EmailMessage()
    message['Subject'] = f'Отключения электричества: {config.label}'
    message['From'] = os.environ.get('SMTP_FROM') or os.environ['SMTP_USER']
    message['To'] = config.email_to
    message.set_content(text)
    try:
        # SMTP over TLS, обычно порт 465; пароль приложения провайдера.
        with smtplib.SMTP_SSL(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT') or '465'),
                              context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise RuntimeError('Не удалось отправить email: проверьте настройки SMTP') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='Файл настроек JSON')
    parser.add_argument('--write-schedules', action='store_true', help='Обновить Actions и systemd из check_time')
    parser.add_argument('--dry-run', action='store_true', help='Показать сводку без отправки')
    parser.add_argument('--chat-id', action='store_true', help='Показать ID приватного чата после /start')
    parser.add_argument('--test-message', action='store_true', help='Отправить проверочное сообщение')
    parser.add_argument('--sample-message', action='store_true', help='Отправить пример сообщения об отключении текущим форматом')
    args = parser.parse_args()
    config = None
    try:
        config = load_config(args.config)
        if args.write_schedules:
            write_schedules(config)
            print(f"Расписания обновлены: {config.check_time} МСК")
            return 0
        if args.chat_id:
            chats = {u['message']['chat']['id'] for u in telegram('getUpdates', {})
                     if u.get('message', {}).get('chat', {}).get('type') == 'private'}
            print('Chat ID:', ', '.join(map(str, sorted(chats))) or 'не найден; напишите боту /start')
            return 0
        if args.test_message:
            send(f'✅ Уведомления об отключениях: {config.label}.', config)
            return 0
        if args.sample_message:
            now = datetime.now(MSK)
            start = now.replace(minute=0, second=0, microsecond=0) + timedelta(days=3, hours=2)
            sample = Outage('SAMPLE', f'п {config.settlement}, ул Пихтовая; п {config.settlement}, ул Благодатная',
                            start, start + timedelta(hours=6), 'Пример комментария с сайта')
            for text in messages([sample], now, config):
                send(text, config)
            return 0
        if not args.dry_run:
            validate_destination(config)
        now = datetime.now(MSK)
        with site_session() as session:
            rows = collect_with_fallback(session, now, config)
        sent = load_sent_state()
        new_rows = filter_unsent(rows, sent)
        if len(new_rows) != len(rows):
            print(f'Уже отправлено ранее, пропущено: {len(rows) - len(new_rows)}')
        for row, text in zip(new_rows, messages(new_rows, now, config)):
            if args.dry_run:
                print(text)
            else:
                send(text, config)
        if not args.dry_run:
            save_sent_state(update_sent_state(sent, new_rows))
        return 0
    except Exception as exc:
        print(f'Ошибка: {type(exc).__name__}: {exc}', file=sys.stderr)
        if config is not None and not args.dry_run and not args.chat_id and not args.test_message and not args.sample_message and not args.write_schedules:
            try:
                send(f'⚠️ Не удалось проверить отключения: {config.label}. Отсутствие сводки не означает отсутствие отключений. Проверьте журнал запуска.', config)
            except Exception:
                print('Не удалось отправить уведомление об ошибке', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
