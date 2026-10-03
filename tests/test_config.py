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
        self.assertEqual(c.check_time, '09:00')
        self.assertEqual(c.settlement, 'Пески')
        self.assertEqual(c.email_to, '')

    def test_custom_settlement_and_district(self):
        c = m.Config(settlement='Рощино', district='Выборгский', res_id='')
        self.assertEqual(len(m.parse_page(page('п Рощино, ул Центральная'), c)[0]), 1)
        self.assertEqual(m.parse_page(page(), c)[0], [])
        self.assertEqual(m.parse_page(page('п Рощино', district='р-н Лужский'), c)[0], [])
        self.assertEqual(c.params['res'], '')

    def test_invalid_config(self):
        for values in [dict(check_time='25:00'), dict(check_time='9:00'), dict(channel='sms'), dict(settlement=''),
                       dict(backup_check_time='25:00'), dict(backup_check_time='9:00')]:
            with self.assertRaises(ValueError):
                m.Config(**values)

    def test_schedules_are_written_in_moscow_time_with_two_slots(self):
        root = Path(m.__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ['.github/workflows/check.yml', 'deploy/peski-monitor.timer']:
                dest = out / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text((root / name).read_text())
            m.write_schedules(m.Config(check_time='01:05', backup_check_time='17:45'), out)
            workflow = (out / '.github/workflows/check.yml').read_text()
            self.assertIn("cron: '5 1 * * *'", workflow)
            self.assertIn("cron: '45 17 * * *'", workflow)
            self.assertEqual(workflow.count('timezone: "Europe/Moscow"'), 2)
            self.assertEqual(workflow.count('cron:'), 2)
            timer = (out / 'deploy/peski-monitor.timer').read_text()
            self.assertIn('OnCalendar=*-*-* 01:05:00 Europe/Moscow', timer)
            self.assertIn('OnCalendar=*-*-* 17:45:00 Europe/Moscow', timer)
            m.write_schedules(m.Config(check_time='08:30'), out)
            self.assertEqual((out / '.github/workflows/check.yml').read_text().count('cron:'), 1)
            self.assertEqual((out / 'deploy/peski-monitor.timer').read_text().count('OnCalendar='), 1)

    def test_committed_schedules_match_config(self):
        c = m.load_config()
        root = Path(m.__file__).parent
        workflow = (root / '.github/workflows/check.yml').read_text()
        timer = (root / 'deploy/peski-monitor.timer').read_text()
        self.assertEqual(workflow.count('cron:'), len(c.check_times), 'Run python monitor.py --write-schedules')
        self.assertEqual(workflow.count('timezone: "Europe/Moscow"'), len(c.check_times))
        for check_time in c.check_times:
            hour, minute = map(int, check_time.split(':'))
            self.assertIn(f"cron: '{minute} {hour} * * *'", workflow, 'Run python monitor.py --write-schedules')
            self.assertIn(f'OnCalendar=*-*-* {check_time}:00 Europe/Moscow', timer)

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
