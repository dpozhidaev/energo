import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import monitor as m
from test_monitor import page


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = m.Config()
        self.assertEqual(c.proxy_workers, 2)
        self.assertEqual(c.settlement, 'Пески')
        self.assertEqual(c.email_to, '')

    def test_custom_settlement_and_district(self):
        c = m.Config(settlement='Рощино', district='Выборгский', res_id='')
        self.assertEqual(len(m.parse_page(page('п Рощино, ул Центральная'), c)[0]), 1)
        self.assertEqual(m.parse_page(page(), c)[0], [])
        self.assertEqual(m.parse_page(page('п Рощино', district='р-н Лужский'), c)[0], [])
        self.assertEqual(c.params['res'], '')

    def test_invalid_config(self):
        for values in [dict(channel='sms'), dict(settlement=''), dict(region_id=344),
                       dict(proxy_workers=0), dict(proxy_workers=21), dict(proxy_workers='2'), dict(proxy_workers=True)]:
            with self.assertRaises(ValueError):
                m.Config(**values)

    def test_committed_config_loads_and_workflow_has_no_schedule(self):
        root = Path(m.__file__).parent
        config = m.load_config()
        self.assertEqual(config.proxy_workers, 2)
        workflow = (root / '.github/workflows/outage-check.yml').read_text(encoding='utf-8')
        self.assertNotIn('cron:', workflow)        # время запуска задаёт внешний планировщик
        self.assertIn('workflow_dispatch:', workflow)

    @patch.dict(m.os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '123'}, clear=True)
    @patch.object(m, 'telegram')
    def test_destination_environment_overrides_config(self, telegram):
        m.send('hello', m.Config(telegram_chat_id='456'))
        self.assertEqual(telegram.call_args.args[1]['chat_id'], '123')

    @patch.dict(m.os.environ, {'SMTP_HOST': 'smtp.example.org', 'SMTP_USER': 'sender@example.org', 'SMTP_PASSWORD': 'test'}, clear=True)
    @patch.object(m.smtplib, 'SMTP_SSL')
    def test_email_destination(self, smtp):
        m.send('test content', m.Config(channel='email', email_to='recipient@example.org'))
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'recipient@example.org')
        self.assertIn('test content', message.get_content())
