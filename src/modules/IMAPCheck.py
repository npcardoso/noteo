import email
from email.header import decode_header
import imaplib
import re
import string
import time
import threading
from xml.sax.saxutils import escape

from Noteo import *
class Mail(object):
    def _wrap(self, text, width):
        return reduce(lambda line, word, width=width: '%s%s%s' %
                      (line,
                       ' \n'[(len(line)-line.rfind('\n')-1
                              + len(word.split('\n',1)[0]
                                ) >= width)],
                       word),
                      text.split(' ')
        )

    def _decode(self, string, config, join=" ", max_items=0, max_len=0):
        if not isinstance(string, basestring):
            return ""

        decoded_header = decode_header(string.strip())

        content = []
        for line in decoded_header:
            max_items = max_items - 1
            if max_items == 0:
                break

            tmp = line[0]
            if line[1] is not None:
                tmp = tmp.decode(line[1], 'replace')
            if max_len:
                tmp = (tmp[:max_len] + '..') if len(tmp) > max_len else tmp
            content.append(tmp)
        ret = join.join(content)

        wrap = config['wrap']
        if wrap > 0:
            ret = ret.splitlines()
            for i in range(len(ret)):
                ret[i] = self._wrap(ret[i], wrap)
            ret = "\n".join(ret)

        return ret

    def _get_content(self, message, config):
        loc = config['linesOfContent']
        if loc == 0:
            return ""

        text = ""
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                text = part.get_payload(decode=True)
                text = text.decode(part.get_content_charset('utf-8'), 'replace')
                break
            elif part.get_content_type() == "text/html":
                text = part.get_payload(decode=True)
                text = text.decode(part.get_content_charset('utf-8'), 'replace')
        text = text.replace('\r', '\n')

        for rexp in config['ignore']:
            text =  re.sub(rexp, '', text,flags=re.MULTILINE)

        text = re.sub('<[^<]+?>', '', text)
        text = re.sub('&[^;]*;', '', text)

        text = re.sub('<', '', text)

        text = re.sub('>', '', text)
        text = re.sub('&', '', text)

        text = [x.strip() for x in text.split('\n') if len(x.strip())]

        if loc > 0:
            text = text [:loc]

        wrap = config['wrap']
        if wrap > 0:
            for i in range(len(text)):
                text[i] = self._wrap(text[i], wrap)
        text = "\n".join(text)
        return text

    def format(self, config):
        sender = self._message['from']
        end_sender = string.find(sender, '<')
        if end_sender >= 0:
            sender = sender[:end_sender]
        sender = sender.strip()
        sender = sender.strip('"')
        sender = self._decode(sender, config)

        subject = self._decode(self._message['subject'], config)
        message = self._get_content(self._message, config)

        content = config['sender_format'] % escape(sender)
        content += config['subject_format'] % escape(subject)
        content += config['message_format'] % escape(message)
        return content

    def __init__(self, message):
        self._message = message


class MailTracker:
    def __init__(self, server, port, ssl, username, password, interval, retries=1):
        self._unread = []
        self._notification_id = None

        self.server = server
        self.port = port
        self.ssl = ssl
        self.username = username
        self.password = password
        self.interval = interval
        self.retries = retries
        self.last_unseen = set()

        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._check)
        self.thread.daemon = True
        self.thread.start()

    def _login(self):
        if self.ssl:
            conn = imaplib.IMAP4_SSL(self.server, self.port)
        else:
            conn = imaplib.IMAP4(self.server, self.port)
        conn.login(self.username,
                   self.password)
        conn.select(readonly=1) # Select inbox or default namespace
        return conn

    def _get_unseen_uids(self, conn):
        (retcode, messages) = conn.search(None, '(UNSEEN)')
        if retcode != 'OK':
            raise imaplib.IMAP4.error("Error return code: %s" % retcode)

        messages = messages[0].strip()
        if len(messages):
            messages = messages.split(' ')
            (retcode, data) = conn.fetch(",".join(messages),'(UID)')
            if retcode != 'OK':
                raise imaplib.IMAP4.error("Error return code: %s" % retcode)
        else:
            data = []

        uid_extracter = re.compile(r'\d* \(UID (\d*)')
        unseen = set()
        for item in data:
            ret = uid_extracter.match(item).group(1)
            unseen.add(int(ret))
        new = unseen - self.last_unseen
        self.last_unseen = unseen & self.last_unseen
        return new

    def _check(self):
        while True:
            for i in range(self.retries + 1):
                try:
                    conn = self._login()

                    #Get UIDs
                    new = self._get_unseen_uids(conn)

                    for uid in new:
                        retcode, data = conn.uid('FETCH', uid, '(RFC822)')#BODY[HEADER.FIELDS (DATE FROM SUBJECT)] BODY[TEXT])')
                        if retcode != 'OK':
                            raise imaplib.IMAP4.error("Error return code: %s" % retcode)
                        content = email.message_from_string(data[0][1])
                        m = Mail(content)
                        with self.lock:
                            self._unread.append(m)
                        self.last_unseen.add(uid)
                    conn.close()
                    conn.logout()
                    break
                except BaseException as e:
                    print("Error mail", e)

            time.sleep(self.interval)

    def check(self, noteo, config):
        with self.lock:
            self._do_stuff(noteo, config)

    def _do_stuff(self, noteo, config):
        if self._notification_id is not None:
            return
        if not self._unread:
            return

        if config['simultaneous'] == 0:
            mails = self._unread
            self._unread = []
        else:
            mails = self._unread[:config['simultaneous']]
            self._unread = self._unread[config['simultaneous']:]

        suffix = ''
        if len(mails) > 1:
            suffix = 's'
        summary = config['header_format'] % (len(mails), suffix, self.username, self.server)


        content = ""
        for m in mails:
            content += m.format(config)


        notification = NotificationEvent(summary,
                                         content,
                                         'mail_new',
                                         timeout=config['notificationTimeout'])

        noteo.add_event(notification)
        self._notification_id = notification.event_id

    def invalidate_event(self, event_id, noteo, config):
        if event_id == self._notification_id:
            self._notification_id = None
        self.check(noteo, config)



class IMAPCheck(NoteoModule):
    config_spec = {
        'checkInterval': 'float(default=120)',
        'notificationTimeout': 'float(default=10)',
        'wrap': 'integer(default=-1)',
        'ignore': 'list(default=list("^\\s*>.*$", "^\\s*[Oo]n.*wrote.*$", "^\s*[^\\s\\b]{10,}\s*$"))',
        'linesOfContent': 'integer(default=-1)',
        'simultaneous': 'integer(default=4)',
        'username': 'list(default=list(username1, username2))',
        'password': 'list(default=list(password1, password2))',
        'server': 'list(default=list(password1, password2))',
        'mailbox': 'list(default=list(inbox, inbox))',
        'port': 'list(default=list(password1, password2))',
        'ssl': 'list(default=list(password1, password2))',
        'header_format': 'string(default="<span size=\"large\"><b>You have <span foreground=\"red\">%d</span> new message%s (%s@%s)</b></span>\n")',
        'sender_format' : 'string(default="<span size=\"large\"><b>From: %s</b></span>\n")',
        'subject_format' : 'string(default="<b>Subject: %s</b>\n")',
        'message_format' : 'string(default="<i>%s</i>\n\n")',
    }

    def init(self):
        update_event = FunctionCallEvent(self.check)
        update_event.recurring_delay = 2

        self._connections = None

        self.noteo.add_event(update_event)

        self.noteo.add_event(FunctionCallEvent(self._create_connections))

        self.noteo.add_event(CreateMenuItemEvent("Check mail now",
                                                 self.check,
                                                 icon='stock_mail')) #TODO: Add conditions to handle this

    def invalidate_event(self, event_id):
        for conn in self._connections:
            conn.invalidate_event(event_id, self.noteo, self.config)

    def check(self):
        self.noteo.logger.debug("Checking mail...")
        if self._connections is None:
            return
        for conn in self._connections:
            conn.check(self.noteo, self.config)


        return True

    def _create_connections(self):
        server = self.config['server']
        port = self.config['port']
        ssl = self.config['ssl']
        username = self.config['username']
        password = self.config['password']

        connections = []
        for i in range(len(server)):
            connections.append(MailTracker(server[i], port[i], ssl[i], username[i], password[i], float(self.config['checkInterval'])))

        self._connections = connections

        self.check()

module = IMAPCheck
