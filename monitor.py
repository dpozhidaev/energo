#!/usr/bin/env python3
"""Ежедневная сводка отключений в Песках. Python 3.11+."""
import argparse
import concurrent.futures
import html
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
import threading
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

CA = Path(__file__).parent / 'certs/russian_trusted_root_ca.pem'
MSK = ZoneInfo('Europe/Moscow')
DEFAULT_CONFIG = Path(__file__).parent / 'config.json'
DEFAULT_SENT_STATE = Path(__file__).parent / 'sent.json'
DEFAULT_RUN_LOG = Path(__file__).parent / 'runs.log'
RUN_LOG_RETENTION = timedelta(days=90)
# Облачные раннеры GitHub Actions размещены за пределами РФ; сайт закрыт DDoS-Guard geoblock'ом
# ("the website owner has restricted access from your current IP address") для таких адресов.
# Список ниже обновляется автоматически (раз в ~30 минут) и включает геолокацию и протокол каждого узла.
MONOSANS_PROXY_LIST_URL = 'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json'
PROXYSCRAPE_URL = 'https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=RU&anonymity=all'
GEONODE_URL = 'https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&country=RU&protocols=http%2Chttps'

# Коды ошибок для сообщения владельцу. Базовые классы сохранены (ValueError/RuntimeError),
# чтобы существующие except и тесты не менялись.
ERROR_TITLES = {
    'E01': 'ни один прокси в РФ не ответил (сайт закрыт напрямую)',
    'E02': 'прокси отвечали, но страницу сайта не отдал ни один (блокировка или заглушка)',
    'E03': 'страница сайта в неожиданном виде (сменилась разметка или заглушка вместо таблицы)',
    'E04': 'не удалось отправить сообщение в Telegram (токен, chat_id или сеть)',
    'E05': 'не удалось отправить email (настройки SMTP)',
    'E06': 'ошибка настроек (config.json или не заданы токен/chat_id)',
    'E98': 'сбой окружения запуска в Actions (установка, тесты или обрыв по времени)',
    'E99': 'неизвестная ошибка',
}


MAX_PROXY_WORKERS = 20


class MonitorError(Exception):
    code = 'E99'


class NoProxyError(MonitorError, RuntimeError):
    code = 'E01'


class ProxiesFailedError(MonitorError, RuntimeError):
    code = 'E02'


class SiteStructureError(MonitorError, ValueError):
    code = 'E03'


class TableMissingError(SiteStructureError):
    """В ответе нет таблицы отключений: через прокси это чаще заглушка, чем смена разметки."""


class TelegramError(MonitorError, RuntimeError):
    code = 'E04'


class EmailError(MonitorError, RuntimeError):
    code = 'E05'


class ConfigError(MonitorError, ValueError):
    code = 'E06'


@dataclass(frozen=True)
class Config:
    url: str = 'https://rosseti-lenenergo.ru/planned_work/'
    settlement: str = 'Пески'
    district: str = 'Выборгский'
    region_id: str = '344'
    res_id: str = '370'
    channel: str = 'telegram'
    telegram_chat_id: str = ''
    email_to: str = ''
    # Сколько прокси проверяется одновременно: столько же параллельных запросов уходит на сайт.
    proxy_workers: int = 2

    def __post_init__(self):
        workers = self.proxy_workers
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_PROXY_WORKERS:
            raise ConfigError(f'proxy_workers должен быть целым числом от 1 до {MAX_PROXY_WORKERS}')
        if not all(isinstance(v, str) for k, v in self.__dict__.items() if k != 'proxy_workers'):
            raise ConfigError('Параметры config.json, кроме proxy_workers, должны быть строками')
        if not self.url.startswith('https://'):
            raise ConfigError('url должен начинаться с https://')
        if self.channel not in ('telegram', 'email'):
            raise ConfigError('channel должен быть telegram или email')
        if not self.settlement.strip() or not self.district.strip():
            raise ConfigError('settlement и district не могут быть пустыми')

    @property
    def params(self):
        return dict(reg=self.region_id, city='', date_start='', date_finish='', res=self.res_id, street='')

    @property
    def source(self):
        return self.url + '?' + urlencode({'reg': self.region_id, 'res': self.res_id})

    @property
    def label(self):
        return f'{self.settlement}, {self.district} район'


def load_config(path=DEFAULT_CONFIG):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return Config(**data)


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
        raise TableMissingError('Таблица отключений не найдена: возможно, сайт изменился или недоступен')
    headers = clean(table.get_text(' ', strip=True)).lower()
    for required in ('адрес', 'плановое время начала', 'плановое время восстановления'):
        if required not in headers:
            raise SiteStructureError('Изменилась структура таблицы отключений')
    outages = []
    for row in table.select('tbody tr'):
        cells = row.find_all('td', recursive=False)
        if not cells:
            continue
        if len(cells) != 11 or not row.get('data-record-id'):
            raise SiteStructureError('Неожиданная строка в таблице; результат не считается пустым')
        vals = [clean(c.get_text(' ', strip=True)) for c in cells]
        # Проверяем район и населённый пункт, а не произвольное упоминание в комментарии.
        addresses = [clean(s) for s in cells[2].get_text('|', strip=True).split('|')]
        if not re.search(r'(?<!\w)' + re.escape(config.district) + r'(?!\w)', vals[1], re.I):
            continue
        matches = [a for a in addresses if settlement.search(a)]
        if not matches:
            continue
        try:
            start = datetime.strptime(vals[3] + ' ' + vals[4], '%d-%m-%Y %H:%M').replace(tzinfo=MSK)
            end = datetime.strptime(vals[5] + ' ' + vals[6], '%d-%m-%Y %H:%M').replace(tzinfo=MSK)
        except ValueError:
            raise SiteStructureError('Неожиданный формат даты или времени в таблице') from None
        if end < start:
            raise SiteStructureError('Время окончания отключения раньше начала')
        # Только строки адреса, где встречается сам посёлок — остальные посёлки общей записи не нужны.
        outages.append(Outage(row['data-record-id'], '; '.join(matches), start, end, vals[9]))
    return outages


# Соединение / ожидание ответа, секунд (в сумме 20): не уложился — берём другой прокси.
REQUEST_TIMEOUT = (6, 14)
# Общий предел на получение страниц (прямой доступ и перебор прокси). Он меньше лимита workflow
# (15 минут), чтобы отчёт владельцу успел уйти, а не оборвался вместе с задачей.
FETCH_BUDGET = 9 * 60


def site_session(proxies=None):
    session = requests.Session()
    session.headers['User-Agent'] = 'PeskiOutageMonitor/1.0 (daily personal notification)'
    if proxies:
        session.proxies.update(proxies)
    return session


SUPPORTED_PROXY_SCHEMES = ('http', 'socks4', 'socks5')
# Для SOCKS имя сайта должно резолвиться на стороне прокси (socks5h/socks4a), а не локально: локальный
# DNS может вернуть заглушку (VPN в режиме fake-ip, подмена), и прокси пойдёт не туда.
REMOTE_DNS_SCHEME = {'socks4': 'socks4a', 'socks5': 'socks5h'}


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


class _Cancelled(Exception):
    """Страница уже получена другим потоком или вышло время."""


def read_outages(session, now, config=Config()):
    """Актуальные записи с первой страницы списка.

    Сайт выводит записи от новых к старым, поэтому будущие и идущие отключения стоят в её начале;
    остальные страницы — архив."""
    response = session.get(config.url, params=config.params, timeout=REQUEST_TIMEOUT, verify=str(CA))
    response.raise_for_status()
    response.encoding = 'utf-8'
    rows = {row.record_id: row for row in parse_page(response.text, config) if row.end > now}
    return sorted(rows.values(), key=lambda r: (r.start, r.record_id))


def collect(session, now, config=Config()):
    rows = read_outages(session, now, config)
    print(f'Актуальных записей ({config.label}): {len(rows)}')
    return rows


def proxy_url(scheme, address):
    return f'{REMOTE_DNS_SCHEME.get(scheme, scheme)}://{address}'


def fetch_via_proxy(scheme, address, now, config, cancel):
    if cancel.is_set():
        raise _Cancelled()
    url = proxy_url(scheme, address)
    with site_session({'http': url, 'https': url}) as session:
        return url, read_outages(session, now, config)


def collect_with_fallback(session, now, config=Config(), budget=FETCH_BUDGET):
    """Сначала прямой доступ; если сайт закрыт — параллельный перебор российских прокси.

    Побеждает первый кандидат, отдавший страницу с таблицей.
    Перебор идёт, пока не кончатся кандидаты или общий предел времени `budget`."""
    deadline = time.monotonic() + budget
    try:
        return collect(session, now, config)
    except TableMissingError:
        pass
    except SiteStructureError:
        raise  # сайт ответил напрямую настоящей таблицей, но в неожиданном виде
    except (requests.RequestException, ValueError):
        pass
    candidates = fetch_candidate_proxies(session)
    if not candidates:
        raise NoProxyError('Не удалось получить список прокси для РФ')
    print(f'Прямой доступ к сайту недоступен; перебираю прокси, кандидатов: {len(candidates)}, '
          f'потоков: {config.proxy_workers}')
    cancel = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=config.proxy_workers)
    tried = reached = structure_errors = 0
    last_error, timed_out = None, False
    try:
        futures = [pool.submit(fetch_via_proxy, scheme, address, now, config, cancel) for scheme, address in candidates]
        try:
            for future in concurrent.futures.as_completed(futures, timeout=max(0, deadline - time.monotonic())):
                tried += 1
                try:
                    url, rows = future.result()
                except (TableMissingError, requests.HTTPError) as exc:
                    reached += 1  # прокси ответил, но не страницей с таблицей
                    last_error = exc
                except SiteStructureError as exc:
                    reached += 1
                    structure_errors += 1
                    last_error = exc
                    if structure_errors >= 2:
                        raise  # два разных прокси отдали таблицу в неожиданном виде — дело в сайте
                except Exception as exc:
                    last_error = exc
                else:
                    print(f'Страница получена через прокси {url} (проверено кандидатов: {tried} из {len(candidates)})')
                    print(f'Актуальных записей ({config.label}): {len(rows)}')
                    return rows
        except concurrent.futures.TimeoutError:
            timed_out = True
    finally:
        # Начатые запросы сами завершатся по таймауту; ждать их не нужно.
        cancel.set()
        pool.shutdown(wait=False, cancel_futures=True)
    detail = f'проверено {tried} из {len(candidates)}' + (f', вышло время ({budget // 60} мин)' if timed_out else '')
    if reached:
        raise ProxiesFailedError(f'Ни один прокси не отдал страницу сайта ({detail}; ответили: {reached})') from last_error
    raise NoProxyError(f'Ни один прокси не ответил ({detail})') from last_error


SENT_STATE_LIMIT = 100


def load_sent_state(path=DEFAULT_SENT_STATE):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []


def save_sent_state(state, path=DEFAULT_SENT_STATE):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sent_key(row):
    """Запись с теми же датами считается уже отправленной; перенос времени — новое уведомление."""
    return f'{row.record_id}|{row.start:%Y-%m-%dT%H:%M}|{row.end:%Y-%m-%dT%H:%M}'


def filter_unsent(rows, sent):
    return [row for row in rows if sent_key(row) not in sent]


def rescheduled_ids(rows, sent):
    """record_id, по которым уведомление уже было, но с другим временем."""
    known = {entry.split('|')[0] for entry in sent}
    return {row.record_id for row in rows if row.record_id in known}


def update_sent_state(sent, new_rows, limit=SENT_STATE_LIMIT):
    """Хранит только последние `limit` отправленных записей, чтобы sent.json не рос бесконечно."""
    return (sent + [sent_key(row) for row in new_rows])[-limit:]


def address_line(row, config=Config()):
    """Строка с улицей, если она есть у посёлка в записи; иначе общий адрес посёлка и района.
    Остальные посёлки общей записи сайта (row.address уже отфильтрован в parse_page) не выводятся."""
    bare = re.compile(r"^(?:п\.?|пос\.?|поселок|посёлок|д\.?|деревня|с\.?|село|г\.?|город)?\s*"
                      + re.escape(config.settlement) + r"\s*$", re.I)
    parts = [a for a in row.address.split('; ') if a]
    if any(not bare.fullmatch(a) for a in parts):
        return '; '.join(parts) + f', {config.district} район'
    return f'пос.{config.label}'


def messages(rows, now, config=Config(), rescheduled=frozenset()):
    # Одно сообщение на запись: интересует только сам факт и время отключения в конкретном
    # посёлке, а не полный (часто на 10+ посёлков) список адресов из ячейки сайта.
    texts = []
    for row in rows:
        # parse_mode HTML только ради жирного заголовка; текст с сайта (адрес, комментарий)
        # экранируем, иначе его спецсимволы сломают разметку или собьют всё сообщение.
        address = html.escape(address_line(row, config))
        comment = html.escape(row.comment) if row.comment else 'не указан'
        title = ('Плановое отключение электричества: изменено время!' if row.record_id in rescheduled
                 else 'Плановое отключение электричества!')
        text = (f'<b>⚡ {title}</b>\n'
                f'{row.start:%d.%m.%Y %H:%M} — {row.end:%d.%m.%Y %H:%M} МСК\n'
                f'Адрес: {address}\n'
                f'Комментарий: {comment}\n'
                f'Запись: {row.record_id}\n'
                f'Проверка: {now:%d.%m.%Y %H:%M} МСК\n'
                f'\nИсточник: {config.source}')
        texts.append(text[:4096])  # лимит Telegram на длину sendMessage
    return texts


def telegram(method, payload):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise ConfigError('Не задан TELEGRAM_BOT_TOKEN')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/{method}', json=payload, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            raise ValueError('Telegram отклонил запрос')
        return data['result']
    except (requests.RequestException, ValueError):
        # Исключение requests содержит URL с токеном — не выводим его в журналы.
        raise TelegramError('Запрос к Telegram не выполнен; проверьте токен, chat_id и доступ к сети') from None


def validate_destination(config):
    if config.channel == 'telegram':
        if not os.environ.get('TELEGRAM_BOT_TOKEN') or not (os.environ.get('TELEGRAM_CHAT_ID') or config.telegram_chat_id):
            raise ConfigError('Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (или telegram_chat_id в config.json)')
    elif not all(os.environ.get(k) for k in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD')) or not config.email_to:
        raise ConfigError('Для email нужны SMTP_HOST, SMTP_USER, SMTP_PASSWORD и email_to')


def send(text, config=Config(), parse_mode=None):
    validate_destination(config)
    if config.channel == 'telegram':
        chat = os.environ.get('TELEGRAM_CHAT_ID') or config.telegram_chat_id
        payload = dict(chat_id=chat, text=text, link_preview_options={'is_disabled': True})
        if parse_mode:
            payload['parse_mode'] = parse_mode
        telegram('sendMessage', payload)
        return
    if parse_mode == 'HTML':
        text = html.unescape(re.sub(r'</?b>', '', text))  # письмо уходит обычным текстом
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
        raise EmailError('Не удалось отправить email: проверьте настройки SMTP') from None


def notify_owner(text, config=Config(), parse_mode=None):
    """Служебные сообщения (ошибки, отчёты о запусках) — только владельцу, не в общий канал.

    Для Telegram получатель — TELEGRAM_ERROR_CHAT_ID; если не задан, сообщение остаётся
    только в журнале запуска. Для email уходит на тот же адрес, что и обычные письма."""
    if config.channel != 'telegram':
        send(text, config, parse_mode)
        return
    chat = os.environ.get('TELEGRAM_ERROR_CHAT_ID', '').strip()
    if not chat:
        print('TELEGRAM_ERROR_CHAT_ID не задан: служебное сообщение не отправлено', file=sys.stderr)
        return
    payload = dict(chat_id=chat, text=text, link_preview_options={'is_disabled': True})
    if parse_mode:
        payload['parse_mode'] = parse_mode
    telegram('sendMessage', payload)


RUN_SOURCES = {'schedule': 'по расписанию', 'workflow_dispatch': 'вручную из Actions'}


def run_source():
    """Что запустило проверку; для расписания добавляется сработавший cron (SCHEDULE_CRON из workflow).

    RUN_TRIGGER — параметр `source` ручного запуска: его передаёт внешний планировщик (cron-job.org),
    чтобы такой запуск не выглядел как нажатие кнопки в Actions."""
    source = RUN_SOURCES.get(os.environ.get('GITHUB_EVENT_NAME', ''), 'локально')
    external = ' '.join(os.environ.get('RUN_TRIGGER', '').split())[:40]
    if external:
        source = f'внешний планировщик {external}'
    cron = os.environ.get('SCHEDULE_CRON', '').strip()
    return f'{source}, cron {cron}' if cron else source


def run_stamp(now):
    return f'Запуск: {now:%d.%m.%Y %H:%M} МСК ({run_source()})'


def run_log_line(now, outcome):
    outcome = ' '.join(outcome.split())[:200]
    return f'{now:%Y-%m-%d %H:%M} МСК | {run_source()} | {outcome}'


def append_run_log(line, now, path=DEFAULT_RUN_LOG):
    """Добавляет строку в журнал запусков и убирает записи старше RUN_LOG_RETENTION (3 месяца)."""
    try:
        old = Path(path).read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        old = []
    kept = []
    for entry in old:
        try:
            when = datetime.strptime(entry[:16], '%Y-%m-%d %H:%M').replace(tzinfo=MSK)
        except ValueError:
            kept.append(entry)
            continue
        if now - when <= RUN_LOG_RETENTION:
            kept.append(entry)
    kept.append(line)
    Path(path).write_text('\n'.join(kept) + '\n', encoding='utf-8')


def record_run(now, outcome):
    # Журнал вторичен: сбой записи не должен ронять уже выполненную проверку.
    try:
        append_run_log(run_log_line(now, outcome), now)
    except OSError:
        print('Не удалось записать журнал запусков', file=sys.stderr)


def error_code(exc):
    return exc.code if isinstance(exc, MonitorError) else 'E99'


def error_report(exc, now, config=Config()):
    code = error_code(exc)
    lines = [f'⚠️ Ошибка {code}: {ERROR_TITLES[code]}',
             f'Не удалось проверить отключения: {config.label}. Отсутствие сводки не означает отсутствие отключений.',
             run_stamp(now),
             f'Детали: {type(exc).__name__}: {exc}']
    if exc.__cause__ is not None:
        lines.append(f'Причина: {type(exc.__cause__).__name__}')
    return '\n'.join(lines)[:1000]


def run_report(now, found, sent, config=Config()):
    return (f'✅ Проверка выполнена: {config.label}\n{run_stamp(now)}\n'
            f'Актуальных записей: {found}, отправлено новых: {sent}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='Файл настроек JSON')
    parser.add_argument('--dry-run', action='store_true', help='Показать сводку без отправки')
    parser.add_argument('--chat-id', action='store_true', help='Показать ID приватного чата после /start')
    parser.add_argument('--test-message', action='store_true', help='Отправить проверочное сообщение')
    parser.add_argument('--sample-message', action='store_true', help='Отправить владельцу пример сообщения об отключении')
    args = parser.parse_args()
    config = None
    try:
        config = load_config(args.config)
        if args.chat_id:
            chats = {u['message']['chat']['id'] for u in telegram('getUpdates', {})
                     if u.get('message', {}).get('chat', {}).get('type') == 'private'}
            print('Chat ID:', ', '.join(map(str, sorted(chats))) or 'не найден; напишите боту /start')
            return 0
        if args.test_message:
            send(f'✅ Уведомления об отключениях: {config.label}.', config)
            if config.channel == 'telegram':
                notify_owner('✅ Канал уведомлений об ошибках настроен.', config)
            return 0
        if args.sample_message:
            now = datetime.now(MSK)
            # Дословно реальная (уже завершённая) запись 332251 с сайта: две улицы Песков.
            sample = Outage('332251', 'п Пески, ул Пихтовая; п Пески, ул Благодатная',
                            datetime(2026, 9, 25, 9, 0, tzinfo=MSK), datetime(2026, 9, 25, 17, 0, tzinfo=MSK),
                            'Замена КТП 2073')
            # Только владельцу: в общем канале пример выглядел бы как настоящее отключение.
            for text in messages([sample], now, config):
                notify_owner(text, config, parse_mode='HTML')
            return 0
        if not args.dry_run:
            validate_destination(config)
        now = datetime.now(MSK)
        print(run_stamp(now))
        with site_session() as session:
            rows = collect_with_fallback(session, now, config)
        sent = load_sent_state()
        new_rows = filter_unsent(rows, sent)
        if len(new_rows) != len(rows):
            print(f'Уже отправлено ранее, пропущено: {len(rows) - len(new_rows)}')
        for row, text in zip(new_rows, messages(new_rows, now, config, rescheduled_ids(new_rows, sent))):
            if args.dry_run:
                print(text)
                continue
            send(text, config, parse_mode='HTML')
            # Сохраняем сразу: если следующая отправка упадёт, эта запись не уйдёт повторно.
            sent = update_sent_state(sent, [row])
            save_sent_state(sent)
        if not args.dry_run:
            record_run(now, f'OK: актуальных {len(rows)}, новых {len(new_rows)}')
            try:
                notify_owner(run_report(now, len(rows), len(new_rows), config), config)
            except Exception:
                # Проверка и рассылка уже выполнены: сбой отчёта владельцу не должен делать запуск «упавшим».
                print('Не удалось отправить отчёт о запуске владельцу', file=sys.stderr)
        return 0
    except Exception as exc:
        print(f'Ошибка {error_code(exc)}: {type(exc).__name__}: {exc}', file=sys.stderr)
        if config is not None and not args.dry_run and not args.chat_id and not args.test_message and not args.sample_message:
            failed_at = datetime.now(MSK)
            record_run(failed_at, f'ОШИБКА {error_code(exc)}: {type(exc).__name__}: {exc}')
            try:
                notify_owner(error_report(exc, failed_at, config), config)
            except Exception:
                print('Не удалось отправить уведомление об ошибке', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
