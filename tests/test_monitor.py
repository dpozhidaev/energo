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
        row1 = m.Outage('1', 'Пески',
                         datetime(2026, 10, 5, 11, 0, tzinfo=m.MSK), datetime(2026, 10, 5, 17, 0, tzinfo=m.MSK),
                         'Плановые работы на линии электропередач')
        row2 = m.Outage('2', 'п Пески, ул Пихтовая; п Пески, ул Благодатная', datetime(2026, 10, 6, 9, 0, tzinfo=m.MSK),
                         datetime(2026, 10, 6, 12, 0, tzinfo=m.MSK), '')
        texts = m.messages([row1, row2], now, config)
        self.assertEqual(texts, [
            '<b>⚡ Плановое отключение электричества!</b>\n'
            '05.10.2026 11:00 — 05.10.2026 17:00 МСК\n'
            'Адрес: пос.Пески, Выборгский район\n'
            'Комментарий: Плановые работы на линии электропередач\n'
            'Запись: 1\n'
            'Проверка: 02.10.2026 09:00 МСК\n'
            '\nИсточник: ' + config.source,
            '<b>⚡ Плановое отключение электричества!</b>\n'
            '06.10.2026 09:00 — 06.10.2026 12:00 МСК\n'
            'Адрес: п Пески, ул Пихтовая; п Пески, ул Благодатная, Выборгский район\n'
            'Комментарий: не указан\n'
            'Запись: 2\n'
            'Проверка: 02.10.2026 09:00 МСК\n'
            '\nИсточник: ' + config.source,
        ])

    def test_messages_escapes_html_in_comment_and_address(self):
        config = m.Config()
        now = datetime(2026, 10, 2, 9, 0, tzinfo=m.MSK)
        row = m.Outage('1', 'п Пески, ул <Центр> & Co', datetime(2026, 10, 5, tzinfo=m.MSK),
                        datetime(2026, 10, 5, 12, tzinfo=m.MSK), 'Авария <важно> & срочно')
        text = m.messages([row], now, config)[0]
        self.assertIn('Адрес: п Пески, ул &lt;Центр&gt; &amp; Co, Выборгский район', text)
        self.assertIn('Комментарий: Авария &lt;важно&gt; &amp; срочно', text)
        self.assertNotIn('<важно>', text)

    def test_real_record_with_two_peski_streets(self):
        # Дословная строка записи 332251 с сайта: две улицы Песков в одной ячейке адреса.
        real_row = '''<tr class="even" data-record-id="332251">
<td>
                        Ленинградская область                    </td>
<td>
                                                р-н Выборгский                    </td>
<td class="rowStreets">
<span>
                            п Пески, ул Пихтовая</span><br/> <span> п Пески, ул Благодатная                        </span>
</td>
<td>
                        25-09-2026                    </td>
<td>
                        09:00                    </td>
<td>
                        25-09-2026                    </td>
<td>
                        17:00                    </td>
<td>
                        Выборгские ЭС                    </td>
<td>
                        Рощинский РЭС                    </td>
<td>
                        Замена КТП 2073                    </td>
<td class="rowFias text-nowrap">
<small>
                                                        1d15fa2f-cf8f-4142-b7ab-e3b32063f7bf</small><br/><small>db020341-c70e-4350-83bb-7438b83dae4e                                                    </small>
</td>
</tr>'''
        html = ('<table class="tableous_facts"><thead><tr><th>Адрес Плановое время начала '
                'Плановое время восстановления</th></tr></thead><tbody>' + real_row + '</tbody></table>')
        config = m.Config()
        rows, _, ids = m.parse_page(html, config)
        self.assertEqual(ids, ('332251',))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].address, 'п Пески, ул Пихтовая; п Пески, ул Благодатная')
        now = datetime(2026, 9, 24, 9, 0, tzinfo=m.MSK)
        texts = m.messages(rows, now, config)
        self.assertEqual(texts, [
            '<b>⚡ Плановое отключение электричества!</b>\n'
            '25.09.2026 09:00 — 25.09.2026 17:00 МСК\n'
            'Адрес: п Пески, ул Пихтовая; п Пески, ул Благодатная, Выборгский район\n'
            'Комментарий: Замена КТП 2073\n'
            'Запись: 332251\n'
            'Проверка: 24.09.2026 09:00 МСК\n'
            '\nИсточник: ' + config.source,
        ])

    def test_address_line_falls_back_for_bare_settlement_mentions(self):
        config = m.Config()
        bare = m.Outage('1', 'Пески', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
        self.assertEqual(m.address_line(bare, config), 'пос.Пески, Выборгский район')
        prefixed = m.Outage('2', 'п. Пески', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
        self.assertEqual(m.address_line(prefixed, config), 'пос.Пески, Выборгский район')

    def test_address_line_shows_streets_when_present(self):
        config = m.Config()
        row = m.Outage('1', 'п Пески, ул Пихтовая; п Пески, ул Благодатная',
                        datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
        self.assertEqual(m.address_line(row, config), 'п Пески, ул Пихтовая; п Пески, ул Благодатная, Выборгский район')

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

    def test_socks_proxies_resolve_names_remotely(self):
        with patch.object(m.requests, 'get', return_value=Mock(text='<table class="tableous_facts">')):
            self.assertEqual(m.probe_proxy('socks4', '1.2.3.4:1080')['https'], 'socks4a://1.2.3.4:1080')
            self.assertEqual(m.probe_proxy('http', '1.2.3.4:8080')['https'], 'http://1.2.3.4:8080')

    @patch.object(m.requests, 'get')
    def test_probe_proxy_checks_real_content(self, get):
        get.return_value = Mock(text='<table class="tableous_facts">...</table>')
        self.assertEqual(m.probe_proxy('socks5', '1.2.3.4:1080'),
                          {'http': 'socks5h://1.2.3.4:1080', 'https': 'socks5h://1.2.3.4:1080'})
        get.return_value = Mock(text='<html>blocked</html>')
        self.assertIsNone(m.probe_proxy('socks5', '1.2.3.4:1080'))
        get.side_effect = requests.exceptions.ConnectTimeout()
        self.assertIsNone(m.probe_proxy('socks5', '1.2.3.4:1080'))

    @patch.object(m, 'probe_proxy')
    @patch.object(m, 'fetch_candidate_proxies')
    def test_iter_working_proxies_yields_only_working_ones(self, fetch_candidates, probe_proxy):
        fetch_candidates.return_value = [('http', '1.1.1.1:80'), ('socks5', '2.2.2.2:1080')]
        probe_proxy.side_effect = lambda scheme, address, config=m.Config(): (
            {'http': f'{scheme}://{address}', 'https': f'{scheme}://{address}'} if address == '2.2.2.2:1080' else None)
        found = list(m.iter_working_proxies(Mock()))
        self.assertEqual([p['https'] for p in found], ['socks5://2.2.2.2:1080'])

    @patch.object(m, 'probe_proxy', return_value=None)
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', '1.1.1.1:80')])
    def test_iter_working_proxies_empty_without_any_match(self, fetch_candidates, probe_proxy):
        self.assertEqual(list(m.iter_working_proxies(Mock())), [])

    @patch.object(m, 'fetch_candidate_proxies', return_value=[])
    def test_iter_working_proxies_raises_without_candidates(self, fetch_candidates):
        with self.assertRaises(RuntimeError):
            list(m.iter_working_proxies(Mock()))

    @patch.object(m, 'iter_working_proxies')
    def test_collect_with_fallback_switches_to_proxy_on_direct_failure(self, iter_proxies):
        proxy = {'http': 'http://1.2.3.4:8080', 'https': 'http://1.2.3.4:8080'}
        iter_proxies.return_value = iter([proxy])
        session = Mock()
        session.proxies = {}
        session.get.side_effect = [requests.exceptions.ConnectionError(), Mock(text=page())]
        rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.proxies, proxy)

    @patch.object(m, 'iter_working_proxies')
    def test_collect_with_fallback_moves_on_when_proxy_times_out(self, iter_proxies):
        slow = {'http': 'socks5://1.1.1.1:1080', 'https': 'socks5://1.1.1.1:1080'}
        good = {'http': 'socks5://2.2.2.2:1080', 'https': 'socks5://2.2.2.2:1080'}
        iter_proxies.return_value = iter([slow, good])
        session = Mock()
        session.proxies = {}
        session.get.side_effect = [requests.exceptions.ConnectionError(),   # напрямую
                                   requests.exceptions.ReadTimeout(),       # первый прокси завис
                                   Mock(text=page())]                       # второй отдал страницу
        rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.proxies, good)

    @patch.object(m, 'iter_working_proxies')
    def test_collect_with_fallback_gives_up_after_max_attempts(self, iter_proxies):
        proxies = [{'http': f'socks5://10.0.0.{i}:1080', 'https': f'socks5://10.0.0.{i}:1080'}
                   for i in range(m.MAX_PROXY_ATTEMPTS + 3)]
        iter_proxies.return_value = iter(proxies)
        session = Mock()
        session.proxies = {}
        session.get.side_effect = requests.exceptions.ReadTimeout()
        with self.assertRaises(RuntimeError):
            m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(session.get.call_count, 1 + m.MAX_PROXY_ATTEMPTS)

    @patch.object(m, 'iter_working_proxies', return_value=iter([]))
    def test_collect_with_fallback_raises_when_no_proxy_found(self, iter_proxies):
        session = Mock()
        session.proxies = {}
        session.get.side_effect = requests.exceptions.ConnectionError()
        with self.assertRaises(RuntimeError):
            m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))

    def test_collect_with_fallback_skips_proxy_search_when_direct_works(self):
        session = Mock()
        session.proxies = {}
        session.get.return_value = Mock(text=page())
        rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.proxies, {})

    def test_requests_use_short_timeout_and_do_not_retry_hangs(self):
        session = Mock()
        session.get.return_value = Mock(text=page())
        m.collect(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(session.get.call_args.kwargs['timeout'], m.REQUEST_TIMEOUT)
        self.assertEqual(m.REQUEST_TIMEOUT[1], 20)
        retries = m.site_session().get_adapter(m.BASE).max_retries
        self.assertEqual((retries.connect, retries.read), (0, 0))

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '@channel',
                               'TELEGRAM_ERROR_CHAT_ID': '555'}, clear=True)
    @patch.object(m, 'telegram')
    def test_send_error_goes_only_to_error_chat(self, telegram):
        m.send_error('boom', m.Config())
        self.assertEqual(telegram.call_count, 1)
        self.assertEqual(telegram.call_args.args[1]['chat_id'], '555')
        self.assertNotIn('parse_mode', telegram.call_args.args[1])

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '@channel'}, clear=True)
    @patch.object(m, 'telegram')
    def test_send_error_never_falls_back_to_the_channel(self, telegram):
        m.send_error('boom', m.Config())
        telegram.assert_not_called()

    @patch.object(m, 'send')
    def test_send_error_for_email_uses_regular_destination(self, send):
        config = m.Config(channel='email', email_to='owner@example.org')
        m.send_error('boom', config)
        send.assert_called_once_with('boom', config)


if __name__ == '__main__':
    unittest.main()
