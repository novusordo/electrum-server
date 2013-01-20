import socket
import sys
import threading
import time
import traceback

from processor import Processor
from utils import Hash, print_log
from version import VERSION


class IrcThread(threading.Thread):

    def __init__(self, processor, config):
        threading.Thread.__init__(self)

        self.processor = processor
        self.daemon = True
        self.stratum_tcp_port = config.get('server', 'stratum_tcp_port')
        self.stratum_http_port = config.get('server', 'stratum_http_port')
        self.stratum_tcp_ssl_port = config.get('server', 'stratum_tcp_ssl_port')
        self.stratum_http_ssl_port = config.get('server', 'stratum_http_ssl_port')
        self.report_stratum_tcp_port = config.get('server', 'report_stratum_tcp_port')
        self.report_stratum_http_port = config.get('server', 'report_stratum_http_port')
        self.report_stratum_tcp_ssl_port = config.get('server', 'report_stratum_tcp_ssl_port')
        self.report_stratum_http_ssl_port = config.get('server', 'report_stratum_http_ssl_port')
        self.peers = {}
        self.host = config.get('server', 'host')
        self.report_host = config.get('server', 'report_host')
        self.nick = config.get('server', 'irc_nick')
        if self.report_stratum_tcp_port:
            self.stratum_tcp_port = self.report_stratum_tcp_port
        if self.report_stratum_http_port:
            self.stratum_http_port = self.report_stratum_http_port
        if self.report_stratum_tcp_ssl_port:
            self.stratum_tcp_ssl_port = self.report_stratum_tcp_ssl_port
        if self.report_stratum_http_ssl_port:
            self.stratum_http_ssl_port = self.report_stratum_http_ssl_port
        if self.report_host:
            self.host = self.report_host
        if not self.nick:
            self.nick = Hash(self.report_host)[:10]
        self.prepend = 'E_'
        if config.get('server', 'coin') == 'litecoin':
            self.prepend = 'EL_'
        self.pruning = config.get('server', 'backend') == 'leveldb'
        self.nick = self.prepend + self.nick

    def get_peers(self):
        return self.peers.values()

    def getname(self):
        s = 'v' + VERSION + ' '
        if self.pruning:
            s += 'p '
        if self.stratum_tcp_port:
            s += 't' + self.stratum_tcp_port + ' '
        if self.stratum_http_port:
            s += 'h' + self.stratum_http_port + ' '
        if self.stratum_tcp_port:
            s += 's' + self.stratum_tcp_ssl_port + ' '
        if self.stratum_http_port:
            s += 'g' + self.stratum_http_ssl_port + ' '
        return s

    def run(self):
        ircname = self.getname()

        while not self.processor.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.settimeout(300)
            except:
                s.close()
                time.sleep(10)
                continue

            try:
                s.send('USER electrum 0 * :' + self.host + ' ' + ircname + '\n')
                s.send('NICK ' + self.nick + '\n')
                s.send('JOIN #electrum\n')
                sf = s.makefile('r', 0)
                t = 0
                while not self.processor.shared.stopped():
                    line = sf.readline().rstrip('\r\n').split()
                    if not line:
                        continue
                    if line[0] == 'PING':
                        s.send('PONG ' + line[1] + '\n')
                    elif '353' in line:  # answer to /names
                        k = line.index('353')
                        for item in line[k+1:]:
                            if item.startswith(self.prepend):
                                s.send('WHO %s\n' % item)
                    elif '352' in line:  # answer to /who
                        # warning: this is a horrible hack which apparently works
                        k = line.index('352')
                        ip = socket.gethostbyname(line[k+4])
                        name = line[k+6]
                        host = line[k+9]
                        ports = line[k+10:]
                        self.peers[name] = (ip, host, ports)
                    if time.time() - t > 5*60:
                        self.processor.push_response({'method': 'server.peers', 'params': [self.get_peers()]})
                        s.send('NAMES #electrum\n')
                        t = time.time()
                        self.peers = {}
            except:
                traceback.print_exc(file=sys.stdout)
            finally:
                sf.close()
                s.close()

        print_log("quitting IRC")


class ServerProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.daemon = True
        self.banner = config.get('server', 'banner')
        self.password = config.get('server', 'password')

        if config.get('server', 'irc') == 'yes':
            self.irc = IrcThread(self, config)
        else:
            self.irc = None

    def get_peers(self):
        if self.irc:
            return self.irc.get_peers()
        else:
            return []

    def run(self):
        if self.irc:
            self.irc.start()
        Processor.run(self)

    def process(self, request):
        method = request['method']
        params = request['params']
        result = None

        if method in ['server.stop', 'server.info']:
            try:
                password = request['params'][0]
            except:
                password = None

            if password != self.password:
                self.push_response({'id': request['id'],
                                    'result': None,
                                    'error': 'incorrect password'})
                return

        if method == 'server.banner':
            result = self.banner.replace('\\n', '\n')

        elif method == 'server.peers.subscribe':
            result = self.get_peers()

        elif method == 'server.version':
            result = VERSION

        elif method == 'server.stop':
            self.shared.stop()
            result = 'stopping, please wait until all threads terminate.'

        elif method == 'server.info':
            result = map(lambda s: {"time": s.time,
                                    "name": s.name,
                                    "address": s.address,
                                    "version": s.version,
                                    "subscriptions": len(s.subscriptions)},
                         self.dispatcher.request_dispatcher.get_sessions())

        elif method == 'server.cache':
            p = self.dispatcher.request_dispatcher.processors['blockchain']
            result = len(repr(p.store.tx_cache))

        elif method == 'server.load':
            p = self.dispatcher.request_dispatcher.processors['blockchain']
            result = p.queue.qsize()

        else:
            print_log("unknown method", request)

        if result != '':
            self.push_response({'id': request['id'], 'result': result})
