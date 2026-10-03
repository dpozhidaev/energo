import tempfile
import threading
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

    def test_filter_unsent_matches_record_and_its_times(self):
        row1 = m.Outage('1', 'addr', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
        row2 = m.Outage('2', 'addr', datetime(2026, 10, 6, tzinfo=m.MSK), datetime(2026, 10, 6, 12, tzinfo=m.MSK), '')
        sent = m.update_sent_state([], [row1])
        self.assertEqual(sent, ['1|2026-10-05T00:00|2026-10-05T12:00'])
        self.assertEqual(m.filter_unsent([row1, row2], sent), [row2])

    def test_rescheduled_record_is_sent_again_and_marked(self):
        old = m.Outage('7', 'Пески', datetime(2026, 10, 5, 11, tzinfo=m.MSK), datetime(2026, 10, 5, 17, tzinfo=m.MSK), '')
        moved = m.Outage('7', 'Пески', datetime(2026, 10, 8, 11, tzinfo=m.MSK), datetime(2026, 10, 8, 17, tzinfo=m.MSK), '')
        fresh = m.Outage('8', 'Пески', datetime(2026, 10, 9, 11, tzinfo=m.MSK), datetime(2026, 10, 9, 17, tzinfo=m.MSK), '')
        sent = m.update_sent_state([], [old])
        new_rows = m.filter_unsent([moved, fresh], sent)
        self.assertEqual(new_rows, [moved, fresh])
        changed = m.rescheduled_ids(new_rows, sent)
        self.assertEqual(changed, {'7'})
        texts = m.messages(new_rows, datetime(2026, 10, 2, 9, tzinfo=m.MSK), m.Config(), changed)
        self.assertTrue(texts[0].startswith('<b>⚡ Плановое отключение электричества: изменено время!</b>\n08.10.2026'))
        self.assertTrue(texts[1].startswith('<b>⚡ Плановое отключение электричества!</b>\n'))

    def test_update_sent_state_keeps_only_last_n(self):
        rows = [m.Outage(str(i), 'addr', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
                for i in range(3)]
        state = m.update_sent_state(['old1', 'old2'], rows, limit=4)
        self.assertEqual(state, ['old2'] + [m.sent_key(r) for r in rows])

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
        self.assertEqual(m.proxy_url('socks5', '1.2.3.4:1080'), 'socks5h://1.2.3.4:1080')
        self.assertEqual(m.proxy_url('socks4', '1.2.3.4:1080'), 'socks4a://1.2.3.4:1080')
        self.assertEqual(m.proxy_url('http', '1.2.3.4:8080'), 'http://1.2.3.4:8080')

    def test_requests_fit_twenty_seconds_and_are_not_retried(self):
        session = Mock()
        session.get.return_value = Mock(text=page())
        m.collect(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(session.get.call_args.kwargs['timeout'], m.REQUEST_TIMEOUT)
        self.assertEqual(sum(m.REQUEST_TIMEOUT), 20)
        self.assertEqual(m.site_session().get_adapter(m.BASE).max_retries.total, 0)
        self.assertLess(m.FETCH_BUDGET, 15 * 60)

    def test_read_pages_reports_progress_and_obeys_cancel(self):
        session = Mock()
        session.get.side_effect = [Mock(text=page(links='<a href="?PAGEN_1=2">2</a>')),
                                   requests.exceptions.ReadTimeout()]
        with self.assertRaises(requests.exceptions.ReadTimeout) as ctx:
            m.read_pages(session, datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertEqual(ctx.exception.pages_read, 1)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(m._Cancelled):
            m.read_pages(Mock(), datetime(2026, 9, 30, tzinfo=m.MSK), cancel=cancel)

    def _direct_blocked_session(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.ConnectionError()
        return session

    def test_collect_with_fallback_skips_proxy_search_when_direct_works(self):
        session = Mock()
        session.get.return_value = Mock(text=page())
        with patch.object(m, 'fetch_candidate_proxies') as fetch:
            rows = m.collect_with_fallback(session, datetime(2026, 9, 30, 18, tzinfo=m.MSK))
        self.assertEqual(len(rows), 1)
        fetch.assert_not_called()

    def test_direct_structure_error_is_reported_without_trying_proxies(self):
        session = Mock()
        session.get.return_value = Mock(text=page().replace('<td>uuid</td>', ''))
        with patch.object(m, 'fetch_candidate_proxies') as fetch:
            with self.assertRaises(m.SiteStructureError) as ctx:
                m.collect_with_fallback(session, datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertEqual(m.error_code(ctx.exception), 'E03')
        fetch.assert_not_called()

    @patch.object(m, 'fetch_via_proxy')
    @patch.object(m, 'fetch_candidate_proxies')
    def test_first_proxy_that_delivers_all_pages_wins(self, fetch_candidates, fetch_via_proxy):
        fetch_candidates.return_value = [('socks5', f'10.0.0.{i}:1080') for i in range(30)]
        row = m.parse_page(page())[0][0]

        def worker(scheme, address, now, config, cancel):
            if address != '10.0.0.17:1080':
                raise requests.exceptions.ConnectTimeout()
            return m.proxy_url(scheme, address), [row], 4
        fetch_via_proxy.side_effect = worker
        rows = m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertEqual(rows, [row])

    @patch.object(m, 'fetch_via_proxy', side_effect=requests.exceptions.ConnectTimeout())
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', '1.1.1.1:80'), ('http', '2.2.2.2:80')])
    def test_e01_when_no_proxy_answers(self, fetch_candidates, fetch_via_proxy):
        with self.assertRaises(m.NoProxyError) as ctx:
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertIn('проверено 2 из 2', str(ctx.exception))

    @patch.object(m, 'fetch_candidate_proxies', return_value=[])
    def test_e01_when_proxy_lists_are_empty(self, fetch_candidates):
        with self.assertRaises(m.NoProxyError):
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))

    @patch.object(m, 'fetch_via_proxy')
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', '1.1.1.1:80'), ('http', '2.2.2.2:80')])
    def test_e02_when_proxies_reach_site_but_stall(self, fetch_candidates, fetch_via_proxy):
        def worker(scheme, address, now, config, cancel):
            exc = requests.exceptions.ReadTimeout()
            exc.pages_read = 2 if address.startswith('1.') else 0
            raise exc
        fetch_via_proxy.side_effect = worker
        with self.assertRaises(m.ProxiesFailedError) as ctx:
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertIn('дошли до сайта: 1', str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, requests.exceptions.ReadTimeout)

    @patch.object(m, 'fetch_via_proxy')
    @patch.object(m, 'fetch_candidate_proxies')
    def test_e03_when_two_proxies_return_unexpected_table(self, fetch_candidates, fetch_via_proxy):
        fetch_candidates.return_value = [('http', f'10.0.0.{i}:80') for i in range(6)]
        fetch_via_proxy.side_effect = m.SiteStructureError('Изменилась структура таблицы отключений')
        with self.assertRaises(m.SiteStructureError) as ctx:
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))
        self.assertEqual(m.error_code(ctx.exception), 'E03')

    @patch.object(m, 'fetch_via_proxy', side_effect=m.TableMissingError('заглушка вместо таблицы'))
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', f'10.0.0.{i}:80') for i in range(6)])
    def test_stub_pages_from_proxies_are_not_a_site_change(self, fetch_candidates, fetch_via_proxy):
        with self.assertRaises(m.NoProxyError):
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK))

    @patch.object(m, 'fetch_via_proxy')
    @patch.object(m, 'fetch_candidate_proxies', return_value=[('http', '1.1.1.1:80'), ('http', '2.2.2.2:80')])
    def test_gives_up_when_time_budget_is_spent(self, fetch_candidates, fetch_via_proxy):
        stopped = []

        def worker(scheme, address, now, config, cancel):
            stopped.append(cancel.wait(5))   # «зависший» прокси ждёт сигнала отмены
            raise m._Cancelled()
        fetch_via_proxy.side_effect = worker
        with self.assertRaises(m.NoProxyError) as ctx:
            m.collect_with_fallback(self._direct_blocked_session(), datetime(2026, 9, 30, tzinfo=m.MSK), budget=0.2)
        self.assertIn('вышло время', str(ctx.exception))
        for thread in threading.enumerate():
            if thread is not threading.current_thread():
                thread.join(2)
        self.assertEqual(stopped, [True, True])   # потокам сообщили об отмене, они не висят

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '@channel',
                               'TELEGRAM_ERROR_CHAT_ID': '555'}, clear=True)
    @patch.object(m, 'telegram')
    def test_notify_owner_goes_only_to_error_chat(self, telegram):
        m.notify_owner('boom', m.Config())
        self.assertEqual(telegram.call_count, 1)
        self.assertEqual(telegram.call_args.args[1]['chat_id'], '555')
        self.assertNotIn('parse_mode', telegram.call_args.args[1])

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '@channel'}, clear=True)
    @patch.object(m, 'telegram')
    def test_notify_owner_never_falls_back_to_the_channel(self, telegram):
        m.notify_owner('boom', m.Config())
        telegram.assert_not_called()

    def test_run_report_shows_time_source_and_counts(self):
        now = datetime(2026, 10, 3, 10, 15, tzinfo=m.MSK)
        with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'schedule'}, clear=True):
            self.assertEqual(m.run_report(now, 3, 1, m.Config()),
                             '✅ Проверка выполнена: Пески, Выборгский район\n'
                             'Запуск: 03.10.2026 10:15 МСК (по расписанию)\n'
                             'Актуальных записей: 3, отправлено новых: 1')
        with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}, clear=True):
            self.assertIn('(вручную из Actions)', m.run_stamp(now))
        with patch.dict(m.os.environ, {}, clear=True):
            self.assertIn('(локально)', m.run_stamp(now))

    def test_error_codes_per_failure_type(self):
        cases = [(m.NoProxyError('x'), 'E01'), (m.ProxiesFailedError('x'), 'E02'),
                 (m.SiteStructureError('x'), 'E03'), (m.TelegramError('x'), 'E04'),
                 (m.EmailError('x'), 'E05'), (m.ConfigError('x'), 'E06'), (KeyError('x'), 'E99')]
        self.assertEqual(m.error_code(m.TableMissingError('x')), 'E03')
        for exc, code in cases:
            self.assertEqual(m.error_code(exc), code)
        self.assertEqual(set(m.ERROR_TITLES), {c for _, c in cases} | {'E98'})

    def test_raised_errors_carry_codes(self):
        with self.assertRaises(m.TableMissingError):
            m.parse_page('<html>Service unavailable</html>')
        with self.assertRaises(m.SiteStructureError):
            m.parse_page(page(end='2026/10/01'))
        with self.assertRaises(m.ConfigError):
            m.Config(channel='sms')
        with patch.dict(m.os.environ, {}, clear=True):
            with self.assertRaises(m.ConfigError):
                m.telegram('getMe', {})

    @patch.object(m.requests, 'post', side_effect=requests.exceptions.ConnectionError('boom'))
    def test_telegram_failure_is_coded_and_hides_token(self, post):
        with patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'secret-token'}, clear=True):
            with self.assertRaises(m.TelegramError) as ctx:
                m.telegram('sendMessage', {})
        self.assertNotIn('secret-token', str(ctx.exception))

    def test_error_report_has_code_title_time_and_cause(self):
        now = datetime(2026, 10, 3, 10, 15, tzinfo=m.MSK)
        try:
            try:
                raise requests.exceptions.ReadTimeout('slow')
            except requests.exceptions.ReadTimeout as cause:
                raise m.ProxiesFailedError('Ни один из 5 прокси не отдал страницы сайта') from cause
        except m.ProxiesFailedError as exc:
            with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'schedule'}, clear=True):
                text = m.error_report(exc, now, m.Config())
        lines = text.split('\n')
        self.assertEqual(lines[0], '⚠️ Ошибка E02: ' + m.ERROR_TITLES['E02'])
        self.assertIn('Запуск: 03.10.2026 10:15 МСК (по расписанию)', lines)
        self.assertIn('Детали: ProxiesFailedError: Ни один из 5 прокси не отдал страницы сайта', lines)
        self.assertEqual(lines[-1], 'Причина: ReadTimeout')

    def test_run_stamp_includes_fired_cron(self):
        now = datetime(2026, 10, 3, 17, 45, tzinfo=m.MSK)
        env = {'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE_CRON': '45 17 * * *'}
        with patch.dict(m.os.environ, env, clear=True):
            self.assertEqual(m.run_stamp(now), 'Запуск: 03.10.2026 17:45 МСК (по расписанию, cron 45 17 * * *)')
            self.assertIn('Запуск: 03.10.2026 17:45 МСК (по расписанию, cron 45 17 * * *)',
                          m.run_report(now, 0, 0, m.Config()))
        with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'SCHEDULE_CRON': ''}, clear=True):
            self.assertEqual(m.run_stamp(now), 'Запуск: 03.10.2026 17:45 МСК (вручную из Actions)')

    def test_external_scheduler_is_named_in_stamp_and_log(self):
        now = datetime(2026, 10, 4, 11, 45, tzinfo=m.MSK)
        env = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'RUN_TRIGGER': 'cron-job.org'}
        with patch.dict(m.os.environ, env, clear=True):
            self.assertEqual(m.run_stamp(now), 'Запуск: 04.10.2026 11:45 МСК (внешний планировщик cron-job.org)')
            self.assertEqual(m.run_log_line(now, 'OK: актуальных 0, новых 0'),
                             '2026-10-04 11:45 МСК | внешний планировщик cron-job.org | OK: актуальных 0, новых 0')
        with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'RUN_TRIGGER': ' a\nb ' + 'x' * 100}, clear=True):
            self.assertNotIn('\n', m.run_source())
            self.assertLessEqual(len(m.run_source()), len('внешний планировщик ') + 40)

    def test_run_log_line_is_single_line_and_bounded(self):
        now = datetime(2026, 10, 3, 17, 45, tzinfo=m.MSK)
        with patch.dict(m.os.environ, {'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE_CRON': '45 17 * * *'}, clear=True):
            self.assertEqual(m.run_log_line(now, 'OK: актуальных 0, новых 0'),
                             '2026-10-03 17:45 МСК | по расписанию, cron 45 17 * * * | OK: актуальных 0, новых 0')
            line = m.run_log_line(now, 'ОШИБКА E02:\nпервая\nвторая ' + 'x' * 500)
        self.assertNotIn('\n', line)
        self.assertLessEqual(len(line.split(' | ')[2]), 200)

    def test_append_run_log_drops_entries_older_than_three_months(self):
        now = datetime(2026, 10, 3, 17, 45, tzinfo=m.MSK)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'runs.log'
            path.write_text('2026-06-01 09:00 МСК | старая\n'          # > 90 дней назад
                            '2026-07-10 09:00 МСК | ещё в пределах\n'   # ~85 дней
                            'строка не по формату\n', encoding='utf-8')
            m.append_run_log('2026-10-03 17:45 МСК | новая', now, path)
            self.assertEqual(path.read_text(encoding='utf-8').splitlines(),
                             ['2026-07-10 09:00 МСК | ещё в пределах', 'строка не по формату',
                              '2026-10-03 17:45 МСК | новая'])
            missing = Path(tmp) / 'new.log'
            m.append_run_log('2026-10-03 17:45 МСК | первая', now, missing)
            self.assertEqual(missing.read_text(encoding='utf-8'), '2026-10-03 17:45 МСК | первая\n')

    def test_sent_state_keeps_last_hundred_by_default(self):
        rows = [m.Outage(str(i), 'a', datetime(2026, 10, 5, tzinfo=m.MSK), datetime(2026, 10, 5, 12, tzinfo=m.MSK), '')
                for i in range(150)]
        state = m.update_sent_state([], rows)
        self.assertEqual(len(state), 100)
        self.assertEqual((state[0], state[-1]), (m.sent_key(rows[50]), m.sent_key(rows[149])))

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '@channel',
                               'TELEGRAM_ERROR_CHAT_ID': '555'}, clear=True)
    @patch.object(m, 'telegram')
    def test_notify_owner_can_send_formatted_text(self, telegram):
        m.notify_owner('<b>x</b>', m.Config(), parse_mode='HTML')
        self.assertEqual(telegram.call_args.args[1]['chat_id'], '555')
        self.assertEqual(telegram.call_args.args[1]['parse_mode'], 'HTML')

    @patch.dict(m.os.environ, {'SMTP_HOST': 'smtp.example.org', 'SMTP_USER': 'a@example.org', 'SMTP_PASSWORD': 'x'}, clear=True)
    @patch.object(m.smtplib, 'SMTP_SSL')
    def test_email_gets_plain_text_instead_of_html(self, smtp):
        m.send('<b>⚡ Заголовок</b>\nАдрес: ул &lt;Центр&gt; &amp; Co', m.Config(channel='email', email_to='x@example.org'),
               parse_mode='HTML')
        body = smtp.return_value.__enter__.return_value.send_message.call_args.args[0].get_content()
        self.assertIn('⚡ Заголовок\nАдрес: ул <Центр> & Co', body)
        self.assertNotIn('<b>', body)

    @patch.object(m, 'send')
    def test_notify_owner_for_email_uses_regular_destination(self, send):
        config = m.Config(channel='email', email_to='owner@example.org')
        m.notify_owner('boom', config)
        send.assert_called_once_with('boom', config, None)


if __name__ == '__main__':
    unittest.main()
