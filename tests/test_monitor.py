import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
import requests
import monitor as m


def page(address='п Пески, ул Пихтовая', district='р-н Выборгский', end='01-10-2026', record='1', links=''):
    values = ['Ленинградская область', district, address, '30-09-2026', '10:00', end, '17:00', 'Выборгские ЭС', 'Рощинский РЭС', '', 'uuid']
    return ('<table class="tableous_facts"><thead><tr><th>Адрес Плановое время начала Плановое время восстановления</th></tr></thead><tbody>'
            + f'<tr data-record-id="{record}">' + ''.join(f'<td>{v}</td>' for v in values)
            + '</tr></tbody></table>' + links)


class Tests(unittest.TestCase):
    def test_multiple_settlements(self):
        self.assertEqual(len(m.parse_page(page('Яппиля<br>Пески<br>Зеркальный'))[0]), 1)

    def test_last_page_broken_next_arrow(self):
        links = '<a href="?PAGEN_1=4">»</a><a class="next" href="?PAGEN_1=5"></a>'
        self.assertEqual(m.parse_page(page(links=links))[1], {1, 2, 3, 4})

    def test_false_matches(self):
        for address in ['п Пескино', 'п Песочный', 'п Рощино, ул Пески', 'СНТ Пески']:
            self.assertEqual(m.parse_page(page(address))[0], [])
        self.assertEqual(m.parse_page(page(district='р-н Лужский'))[0], [])

    def test_markup_failure(self):
        with self.assertRaises(ValueError):
            m.parse_page('<html>Service unavailable</html>')
        with self.assertRaises(ValueError):
            m.parse_page(page().replace('<td>uuid</td>', ''))

    def test_pages_preserve_filters_and_expired(self):
        session = Mock()
        responses = [page(end='30-09-2026', links='<a href="?PAGEN_1=2">2</a>'), page(record='2')]
        session.get.side_effect = [Mock(text=text) for text in responses]
        rows = m.collect(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual([r.record_id for r in rows], ['2'])
        self.assertEqual(session.get.call_args.kwargs['params']['res'], '370')
        self.assertEqual(session.get.call_args.kwargs['params']['PAGEN_1'], 2)

    def test_repeated_page_is_error(self):
        session = Mock()
        session.get.return_value = Mock(text=page(links='<a href="?PAGEN_1=2">2</a>'))
        with self.assertRaises(ValueError):
            m.collect(session, datetime(2026, 9, 30, tzinfo=m.MSK))

    def test_no_results_no_messages(self):
        self.assertEqual(m.messages([], datetime.now(m.MSK)), [])

    def test_sent_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sent.json'
            self.assertEqual(m.load_sent_state(path), [])
            m.save_sent_state(['1', '2'], path)
            self.assertEqual(m.load_sent_state(path), ['1', '2'])

    def test_filter_unsent_skips_known_record_ids(self):
        row1 = m.Outage('1', 'addr', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
        row2 = m.Outage('2', 'addr', datetime(2026, 10, 6, tzinfo=m.MSK), datetime(2026, 10, 6, 12, tzinfo=m.MSK), '')
        self.assertEqual(m.filter_unsent([row1, row2], ['1']), [row2])

    def test_update_sent_state_keeps_only_last_n(self):
        rows = [m.Outage(str(i), 'addr', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
                for i in range(3)]
        sent = ['old1', 'old2']
        self.assertEqual(m.update_sent_state(sent, rows, limit=4), ['old2', '0', '1', '2'])

    def test_messages_one_per_record_with_fixed_address(self):
        config = m.Config()
        now = datetime(2026, 10, 2, 9, 0, tzinfo=m.MSK)
        row1 = m.Outage('1', 'раздельный реальный адрес с сайта тут не используется',
                         datetime(2026, 10, 5, 11, 0, tzinfo=m.MSK), datetime(2026, 10, 5, 17, 0, tzinfo=m.MSK),
                         'Плановые работы на линии электропередач')
        row2 = m.Outage('2', 'другой адрес', datetime(2026, 10, 6, 9, 0, tzinfo=m.MSK),
                         datetime(2026, 10, 6, 12, 0, tzinfo=m.MSK), '')
        texts = m.messages([row1, row2], now, config)
        self.assertEqual(texts, [
            'Плановое отключение электричества!\n'
            '05.10.2026 11:00 — 05.10.2026 17:00 МСК\n'
            'Адрес: пос.Пески, Выборгский район\n'
            'Комментарий: Плановые работы на линии электропередач\n'
            'Запись: 1\n'
            'Проверка: 02.10.2026 09:00 МСК\n'
            '\nИсточник: ' + config.source,
            'Плановое отключение электричества!\n'
            '06.10.2026 09:00 — 06.10.2026 12:00 МСК\n'
            'Адрес: пос.Пески, Выборгский район\n'
            'Комментарий: не указан\n'
            'Запись: 2\n'
            'Проверка: 02.10.2026 09:00 МСК\n'
            '\nИсточник: ' + config.source,
        ])

    def test_messages_truncated_to_telegram_limit(self):
        row = m.parse_page(page())[0][0]
        long_comment = m.Outage(row.record_id, row.address, row.start, row.end, 'а' * 10000)
        texts = m.messages([long_comment], datetime.now(m.MSK))
        self.assertEqual(len(texts), 1)
        self.assertLessEqual(len(texts[0]), 4096)

    def test_fetch_candidate_proxies_parses_and_dedupes_sources(self):
        session = Mock()
        monosans = [
            {'protocol': 'socks5', 'host': '9.9.9.9', 'port': 1080, 'timeout': 0.5,
             'geolocation': {'country': {'iso_code': 'RU'}}},
            {'protocol': 'socks5', 'host': '8.8.8.8', 'port': 1080, 'timeout': 0.1,
             'geolocation': {'country': {'iso_code': 'RU'}}},
            {'protocol': 'http', 'host': '7.7.7.7', 'port': 80, 'geolocation': {'country': {'iso_code': 'DE'}}},
        ]
        session.get.side_effect = [
            Mock(json=Mock(return_value=monosans)),
            Mock(text='1.2.3.4:8080\nnot-a-proxy\n5.6.7.8:3128\n'),
            Mock(json=Mock(return_value={'data': [{'ip': '5.6.7.8', 'port': 3128}, {'ip': '9.9.9.9', 'port': 80}]})),
        ]
        candidates = m.fetch_candidate_proxies(session)
        self.assertEqual(candidates, [
            ('socks5', '8.8.8.8:1080'), ('socks5', '9.9.9.9:1080'),
            ('http', '1.2.3.4:8080'), ('http', '5.6.7.8:3128'), ('http', '9.9.9.9:80'),
        ])

    def test_fetch_candidate_proxies_ignores_source_errors(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.RequestException()
        self.assertEqual(m.fetch_candidate_proxies(session), [])

    @patch.object(m.requests, 'get')
    def test_probe_proxy_checks_real_content(self, get):
        get.return_value = Mock(text='<table class="tableous_facts">...</table>')
        self.assertEqual(m.probe_proxy('socks5', '1.2.3.4:1080'),
                          {'http': 'socks5://1.2.3.4:1080', 'https': 'socks5://1.2.3.4:1080'})
        get.return_value = Mock(text='<html>blocked</html>')
        self.assertIsNone(m.probe_proxy('socks5', '1.2.3.4:1080'))
        get.side_effect = requests.exceptions.ConnectTimeout()
        self.assertIsNone(m.probe_proxy('socks5', '1.2.3.4:1080'))

    @patch.object(m, 'probe_proxy')
    @patch.object(m, 'fetch_candidate_proxies')
    def test_find_working_proxy_returns_first_success(self, fetch_candidates, probe_proxy):
        fetch_candidates.return_value = [('http', '1.1.1.1:80'), ('socks5', '2.2.2.2:1080')]
        probe_proxy.side_effect = lambda scheme, address, config=m.Config(): (
            {'http': f'{scheme}://{address}', 'https': f'{scheme}://{address}'} if address == '2.2.2.2:1080' else None)
        self.assertEqual(m.find_working_proxy(Mock())['https'], 'socks5://2.2.2.2:1080')

    @patch.object(m, 'probe_proxy', return_value=None)
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', '1.1.1.1:80')])
    def test_find_working_proxy_raises_without_any_match(self, fetch_candidates, probe_proxy):
        with self.assertRaises(RuntimeError):
            m.find_working_proxy(Mock())

    @patch.object(m, 'fetch_candidate_proxies', return_value=[])
    def test_find_working_proxy_raises_without_candidates(self, fetch_candidates):
        with self.assertRaises(RuntimeError):
            m.find_working_proxy(Mock())

    @patch.object(m, 'find_working_proxy')
    def test_collect_with_fallback_switches_to_proxy_on_direct_failure(self, find_working_proxy):
        find_working_proxy.return_value = {'http': 'http://1.2.3.4:8080', 'https': 'http://1.2.3.4:8080'}
        session = Mock()
        session.proxies = {}
        session.get.side_effect = [requests.exceptions.ConnectionError(), Mock(text=page())]
        rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.proxies, find_working_proxy.return_value)

    def test_collect_with_fallback_skips_proxy_search_when_direct_works(self):
        session = Mock()
        session.proxies = {}
        session.get.return_value = Mock(text=page())
        rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.proxies, {})


if __name__ == '__main__':
    unittest.main()
